"""TLS-only SMTP delivery using encrypted opaque recipient targets."""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass, field
from email.headerregistry import Address
from email.message import EmailMessage
from typing import Protocol

from zhixu.adapters.channels.targets import OutboundTargetStore
from zhixu.channels import ChannelCapabilities, ChannelDeliveryResult, OutboundMessage
from zhixu.domain import DataClassification
from zhixu.domain.errors import ValidationError


@dataclass(frozen=True, slots=True)
class EmailCredentials:
    channel_account: str
    host: str = field(repr=False)
    port: int
    sender: str = field(repr=False)
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    implicit_tls: bool = True

    def __post_init__(self) -> None:
        if (
            not self.channel_account.strip()
            or not self.host.strip()
            or not self.sender.strip()
        ):
            raise ValidationError("email credentials are incomplete")
        if not 1 <= self.port <= 65535:
            raise ValidationError("SMTP port is invalid")
        _mailbox(self.sender)
        if bool(self.username) != bool(self.password):
            raise ValidationError("SMTP username and password must be configured together")


class SMTPTransport(Protocol):
    def send(
        self,
        message: EmailMessage,
        *,
        credentials: EmailCredentials,
        timeout: float,
    ) -> tuple[bool, str]: ...


class SmtplibTransport:
    def send(
        self,
        message: EmailMessage,
        *,
        credentials: EmailCredentials,
        timeout: float,
    ) -> tuple[bool, str]:
        context = ssl.create_default_context()
        try:
            if credentials.implicit_tls:
                client = smtplib.SMTP_SSL(
                    credentials.host,
                    credentials.port,
                    timeout=timeout,
                    context=context,
                )
            else:
                client = smtplib.SMTP(
                    credentials.host,
                    credentials.port,
                    timeout=timeout,
                )
            with client:
                if not credentials.implicit_tls:
                    client.ehlo()
                    client.starttls(context=context)
                    client.ehlo()
                if credentials.username:
                    client.login(credentials.username, credentials.password)
                refused = client.send_message(message)
        except smtplib.SMTPResponseException as exc:
            return False, f"smtp_{exc.smtp_code}"
        except (OSError, smtplib.SMTPException):
            return False, "smtp_unavailable"
        return (False, "recipient_rejected") if refused else (True, "ok")


class EmailOutboundAdapter:
    channel = "email"
    capabilities = ChannelCapabilities(
        inbound_text=False,
        outbound_text=True,
        proactive_push=True,
    )

    def __init__(
        self,
        credentials: EmailCredentials,
        targets: OutboundTargetStore,
        *,
        transport: SMTPTransport | None = None,
        subject: str = "知序通知",
        timeout: float = 10,
    ) -> None:
        if "\r" in subject or "\n" in subject or not subject.strip():
            raise ValidationError("email subject is invalid")
        self.credentials = credentials
        self.targets = targets
        self.transport = transport or SmtplibTransport()
        self.subject = subject
        self.timeout = timeout

    @property
    def channel_account(self) -> str:
        return self.credentials.channel_account

    def send(self, message: OutboundMessage) -> ChannelDeliveryResult:
        if (
            message.channel != self.channel
            or message.channel_account != self.channel_account
        ):
            return ChannelDeliveryResult(False, False, "account_mismatch")
        if message.classification >= DataClassification.CONFIDENTIAL:
            return ChannelDeliveryResult(False, False, "classification_blocked")
        target = self.targets.resolve(
            channel=self.channel,
            channel_account=self.channel_account,
            opaque_ref=message.target_ref,
        )
        if target.kind != "recipient":
            return ChannelDeliveryResult(False, False, "target_kind_unsupported")
        recipient = _mailbox(target.value)
        sender = _mailbox(self.credentials.sender)
        mail = EmailMessage()
        mail["From"] = sender
        mail["To"] = recipient
        mail["Subject"] = self.subject
        mail.set_content(message.text)
        ok, code = self.transport.send(
            mail,
            credentials=self.credentials,
            timeout=self.timeout,
        )
        return ChannelDeliveryResult(
            ok,
            retryable=not ok and code in {"smtp_unavailable", "smtp_421", "smtp_450", "smtp_451"},
            provider_code=code,
        )


def _mailbox(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValidationError("email address contains a header delimiter")
    try:
        address = Address(addr_spec=value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("email address is invalid") from exc
    if not address.username or not address.domain:
        raise ValidationError("email address is invalid")
    return str(address)
