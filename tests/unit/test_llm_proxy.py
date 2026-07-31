from __future__ import annotations

import pytest

from zhixu.runtime.llm_proxy import _extract_web_answer, _safe_web_source


def test_web_answer_extracts_final_text_and_safe_deduplicated_sources() -> None:
    answer, sources = _extract_web_answer(
        {
            "content": [
                {"type": "text", "text": "正在检索。"},
                {
                    "type": "web_search_tool_result",
                    "content": [
                        {
                            "type": "web_search_result",
                            "title": "Official source",
                            "url": "https://example.com/fact",
                        },
                        {
                            "type": "web_search_result",
                            "title": "Duplicate",
                            "url": "https://example.com/fact",
                        },
                        {
                            "type": "web_search_result",
                            "title": "Private",
                            "url": "http://127.0.0.1/private",
                        },
                    ],
                },
                {
                    "type": "text",
                    "text": "这是基于检索结果的答案。",
                    "citations": [
                        {
                            "title": "Second source",
                            "url": "https://example.org/second",
                        }
                    ],
                },
            ]
        }
    )

    assert answer == "这是基于检索结果的答案。"
    assert sources == [
        {"title": "Official source", "url": "https://example.com/fact"},
        {"title": "Second source", "url": "https://example.org/second"},
    ]


def test_web_answer_requires_final_text() -> None:
    with pytest.raises(ValueError):
        _extract_web_answer({"content": []})


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/private",
        "https://user:pass@example.com/private",
        "https://example.com/page#fragment",
    ],
)
def test_web_source_rejects_unsafe_urls(url: str) -> None:
    assert _safe_web_source("source", url) is None
