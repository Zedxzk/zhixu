from __future__ import annotations

import pytest

from zhixu.security import (
    SecretRedactor,
    contains_financial_credential,
    web_query_is_safe,
)

# Every value here is obviously synthetic; the release gate scans for secrets.


@pytest.mark.parametrize(
    "text",
    [
        "记一下我的银行卡密码是 000000",
        "支付密码 是 111111",
        "信用卡 CVV 是 000",
        "招商银行卡的密码：synthetic-pin-0",
        "网上银行密码为 000000",
        "取款密码=222222",
    ],
)
def test_a_stated_financial_credential_is_refused(text: str) -> None:
    assert contains_financial_credential(text) is True


@pytest.mark.parametrize(
    "text",
    [
        # A financial noun with no value at all.
        "银行卡密码忘了怎么办",
        "怎么修改支付密码",
        "明天去银行改密码",
        # A financial noun, a separator, but a value written in prose.
        "支付密码是多少我也不记得了",
        # A credential with a value, but nothing financial about it.
        "记一下会议室密码是 synthetic-room-0",
        "家里WiFi密码是 synthetic-wifi-0",
        # A financial noun next to a number that is not a credential.
        "银行卡余额是 1234",
    ],
)
def test_ordinary_text_is_not_mistaken_for_a_financial_credential(text: str) -> None:
    """Three signals must coincide, so none of these may be refused."""

    assert contains_financial_credential(text) is False


def test_a_value_is_hidden_but_its_label_survives() -> None:
    """The label has to stay, or the model cannot title or file the note."""

    redactor = SecretRedactor()
    hidden = redactor.redact("家里WiFi密码是 synthetic-wifi-0")

    assert "synthetic-wifi-0" not in hidden
    assert "<SECRET_1>" in hidden
    assert "WiFi密码是" in hidden
    assert redactor.restore(hidden) == "家里WiFi密码是 synthetic-wifi-0"


def test_a_vendor_token_is_hidden_without_any_label() -> None:
    redactor = SecretRedactor()
    hidden = redactor.redact("备用 sk-synthetic1234567890 放这里")

    assert "sk-synthetic1234567890" not in hidden
    assert redactor.restore(hidden) == "备用 sk-synthetic1234567890 放这里"


def test_one_redactor_numbers_several_texts_consistently() -> None:
    """A staged plan is replayed alongside the new message; both must agree."""

    redactor = SecretRedactor()
    first = redactor.redact("密码是 synthetic-value-a")
    second = redactor.redact("另一个密码是 synthetic-value-b")

    assert "<SECRET_1>" in first
    assert "<SECRET_2>" in second
    assert redactor.restore(second) == "另一个密码是 synthetic-value-b"


def test_a_placeholder_the_user_typed_cannot_be_restored() -> None:
    """Otherwise a user could forge one and steer restoration."""

    redactor = SecretRedactor()
    forged = redactor.redact("我自己打的 <SECRET_1> 占位符")

    assert redactor.restore(forged) == forged
    assert "<SECRET_1>" not in forged


def test_scheduled_text_is_blanked_rather_than_restored() -> None:
    """Restoring in a notification would broadcast the value on a timer."""

    redactor = SecretRedactor()
    hidden = redactor.redact("密码是 synthetic-value-c")

    assert redactor.blank(hidden) == "密码是 （已隐藏）"
    assert "synthetic-value-c" not in (redactor.blank(hidden) or "")


def test_the_cap_leaves_extra_values_alone() -> None:
    redactor = SecretRedactor()
    text = "，".join(f"密码是 synthetic-value-{index}" for index in range(9))
    hidden = redactor.redact(text)

    assert hidden.count("<SECRET_") == 8
    assert "synthetic-value-8" in hidden


def test_the_web_gate_keeps_its_wider_reach() -> None:
    """Sharing the credential patterns must not narrow the search screen."""

    assert web_query_is_safe("synthetic.person@example.invalid 是谁") is False
    assert web_query_is_safe("密码是 synthetic-secret-value") is False
    assert web_query_is_safe("sk-synthetic1234567890 有效吗") is False
    assert web_query_is_safe("天空为什么是蓝色的") is True


def test_a_personal_identifier_alone_is_not_a_stored_credential() -> None:
    """The web gate blocks an email; the storage path must not redact one."""

    redactor = SecretRedactor()
    text = "同事的邮箱是 synthetic.person@example.invalid"

    assert web_query_is_safe(text) is False
    assert redactor.redact(text) == text
