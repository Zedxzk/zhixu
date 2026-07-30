"""Length-bounded JSON RPC over an authenticated Unix domain socket."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import stat
import struct
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .crypto import VaultKeyring
from .policy import SecretKind, VaultAction, VaultClassification
from .service import VaultService
from .types import SecretValue
from .webauthn_auth import PasskeyManager

MAX_FRAME_BYTES = 64 * 1024


class VaultRPCDispatcher:
    def __init__(
        self,
        service: VaultService,
        keyring: VaultKeyring,
        *,
        passkeys: PasskeyManager | None = None,
    ) -> None:
        self.service = service
        self.keyring = keyring
        self.passkeys = passkeys

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        method = str(request.get("method") or "")
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        if method == "status":
            return {"sealed": self.keyring.sealed}
        if method == "lock":
            self.keyring.lock()
            return {"sealed": True}
        if method == "unlock":
            passphrase = str(params.get("passphrase") or "")
            if not passphrase:
                raise PermissionError("vault passphrase is required")
            self.keyring.unlock(passphrase)
            return {"sealed": False}
        if method == "passkey_begin_registration":
            options = self._passkeys().begin_registration(
                user_id=str(params.get("user_id") or ""),
                user_name=str(params.get("user_name") or ""),
                display_name=str(params.get("display_name") or ""),
            )
            return {"options": json.loads(options)}
        if method == "passkey_finish_registration":
            credential = params.get("credential")
            if not isinstance(credential, dict):
                raise ValueError("Passkey credential must be an object")
            credential_id = self._passkeys().finish_registration(
                user_id=str(params.get("user_id") or ""),
                credential=credential,
            )
            return {"credential_id": credential_id}
        if method == "passkey_begin_authentication":
            options = self._passkeys().begin_authentication(
                user_id=str(params.get("user_id") or ""),
            )
            return {"options": json.loads(options)}
        if method == "passkey_finish_authentication":
            credential = params.get("credential")
            if not isinstance(credential, dict):
                raise ValueError("Passkey credential must be an object")
            proof = self._passkeys().finish_authentication(
                user_id=str(params.get("user_id") or ""),
                credential=credential,
            )
            return {
                "user_id": proof.user_id,
                "credential_id": proof.credential_id,
                "authenticated_at": proof.authenticated_at.isoformat(),
                "expires_at": proof.expires_at.isoformat(),
            }
        if method == "create":
            classification_value = params.get("classification")
            kind = SecretKind(str(params.get("kind") or ""))
            classification = (
                VaultClassification(str(classification_value))
                if classification_value is not None
                else None
            )
            value = SecretValue.from_text(str(params.get("value") or ""))
            try:
                metadata = self.service.create_with_grant(
                    str(params.get("grant") or ""),
                    secret_id=str(params.get("secret_id") or ""),
                    owner_user_id=str(params.get("owner_user_id") or ""),
                    label=str(params.get("label") or ""),
                    kind=kind,
                    value=value,
                    classification=classification,
                )
            finally:
                value.clear()
            return {"item": self._metadata(metadata)}
        if method == "list_owned_metadata":
            values = self.service.list_owned_metadata(
                str(params.get("grant") or ""),
            )
            return {"items": [self._metadata(item) for item in values]}
        if method == "list_metadata":
            values = self.service.list_metadata(
                str(params.get("grant") or ""),
                str(params.get("secret_id") or ""),
            )
            return {
                "items": [
                    {
                        **asdict(item),
                        "kind": item.kind.value,
                        "classification": item.classification.value,
                        "created_at": item.created_at.isoformat(),
                        "updated_at": item.updated_at.isoformat(),
                    }
                    for item in values
                ]
            }
        if method == "use":
            result = self.service.use(
                str(params.get("grant") or ""),
                str(params.get("secret_id") or ""),
                executor_name=str(params.get("executor") or ""),
                request=(
                    params.get("request")
                    if isinstance(params.get("request"), dict)
                    else {}
                ),
            )
            return {"ok": result.ok, "code": result.code, "data": result.data}
        if method == "reveal":
            with self.service.reveal(
                str(params.get("grant") or ""),
                str(params.get("secret_id") or ""),
            ) as secret:
                return {"value": secret.text()}
        if method == "update":
            expected_version = int(params.get("expected_version") or 0)
            value = SecretValue.from_text(str(params.get("value") or ""))
            try:
                metadata = self.service.update(
                    str(params.get("grant") or ""),
                    str(params.get("secret_id") or ""),
                    value,
                    expected_version=expected_version,
                )
            finally:
                value.clear()
            return {"item": self._metadata(metadata)}
        if method == "delete":
            self.service.delete(
                str(params.get("grant") or ""),
                str(params.get("secret_id") or ""),
            )
            return {"deleted": True}
        if method == "export":
            payload = self.service.export_encrypted(
                str(params.get("grant") or ""),
                str(params.get("secret_id") or ""),
                export_passphrase=str(params.get("export_passphrase") or ""),
            )
            return {"encrypted_export": base64.urlsafe_b64encode(payload).decode()}
        if method == "grant":
            self.service.grant_acl(
                str(params.get("grant") or ""),
                str(params.get("secret_id") or ""),
                subject=str(params.get("subject") or ""),
                action=VaultAction(str(params.get("action") or "")),
            )
            return {"granted": True}
        raise ValueError("unsupported vault RPC method")

    def _passkeys(self) -> PasskeyManager:
        if self.passkeys is None:
            raise RuntimeError("Passkey service is unavailable")
        return self.passkeys

    @staticmethod
    def _metadata(item: Any) -> dict[str, Any]:
        value = asdict(item)
        value["kind"] = item.kind.value
        value["classification"] = item.classification.value
        value["created_at"] = item.created_at.isoformat()
        value["updated_at"] = item.updated_at.isoformat()
        return value


class UnixVaultServer:
    def __init__(
        self,
        path: str | Path,
        dispatcher: VaultRPCDispatcher,
        *,
        allowed_uids: set[int],
        socket_gid: int | None = None,
    ) -> None:
        if not allowed_uids:
            raise ValueError("at least one allowed Unix UID is required")
        self.path = Path(path)
        self.dispatcher = dispatcher
        self.allowed_uids = allowed_uids
        self.socket_gid = socket_gid
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        if self.path.exists():
            mode = self.path.lstat().st_mode
            if not stat.S_ISSOCK(mode):
                raise FileExistsError("vault socket path is occupied by a non-socket")
            self.path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle,
            path=self.path,
            limit=MAX_FRAME_BYTES,
        )
        if self.socket_gid is not None:
            os.chown(self.path, -1, self.socket_gid)
        os.chmod(self.path, 0o660)

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            peer_socket = writer.get_extra_info("socket")
            if peer_socket is None or self._peer_uid(peer_socket) not in self.allowed_uids:
                await self._reply(writer, {"ok": False, "error": "peer_denied"})
                return
            raw = await reader.readline()
            if not raw or len(raw) > MAX_FRAME_BYTES:
                await self._reply(writer, {"ok": False, "error": "invalid_frame"})
                return
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            result = self.dispatcher.dispatch(request)
            await self._reply(writer, {"ok": True, "result": result})
        except Exception as exc:
            await self._reply(
                writer,
                {"ok": False, "error": type(exc).__name__},
            )
        finally:
            writer.close()
            await writer.wait_closed()

    @staticmethod
    def _peer_uid(peer_socket: socket.socket) -> int:
        if not hasattr(socket, "SO_PEERCRED"):
            raise RuntimeError("Unix peer credentials are unavailable")
        credentials = peer_socket.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            struct.calcsize("3i"),
        )
        _pid, uid, _gid = struct.unpack("3i", credentials)
        return int(uid)

    @staticmethod
    async def _reply(
        writer: asyncio.StreamWriter,
        payload: dict[str, Any],
    ) -> None:
        writer.write(
            json.dumps(payload, separators=(",", ":")).encode() + b"\n"
        )
        await writer.drain()
