from __future__ import annotations

import pytest

from zhixu.security import web_query_is_safe


@pytest.mark.parametrize(
    "query",
    [
        "请搜索 2026 年公开发布的 Python 版本",
        "密码管理器的工作原理是什么",
        "搜索 https://docs.python.org 的官方说明",
    ],
)
def test_public_web_queries_are_allowed(query: str) -> None:
    assert web_query_is_safe(query)


@pytest.mark.parametrize(
    "query",
    [
        "搜索 user@example.com 的资料",
        "我的密码是 synthetic-secret-value",
        "查询 6222 0200 0000 0000 000",
        "访问 http://localhost/admin",
        "解释 sk-synthetic1234567890 的权限",
        "打开 https://example.com/?token=synthetic",
        "检查 abcdefghijklmnopqrstuv1234567890",
    ],
)
def test_sensitive_web_queries_are_blocked(query: str) -> None:
    assert not web_query_is_safe(query)
