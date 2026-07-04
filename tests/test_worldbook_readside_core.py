from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import worldbook_match  # noqa: E402
import worldbook_readside_core as readside  # noqa: E402


def _entry(
    entry_id: str,
    *,
    name: str = "",
    keywords: list[str] | None = None,
    content: str = "content",
    enabled: bool = True,
    always_on: bool = False,
) -> dict:
    return {
        "id": entry_id,
        "name": name,
        "keywords": keywords or [],
        "content": content,
        "enabled": enabled,
        "alwaysOn": always_on,
    }


def _messages(*texts: str) -> list[dict]:
    return [{"role": "user", "content": text} for text in texts]


def test_build_block_matches_worldbook_match_and_reports_names():
    entries = [
        _entry("wb1", name="io项目", keywords=["io"], content="io 是产品"),
        _entry("wb2", name="无关", keywords=["zzz"], content="no"),
        _entry("wb3", name="", always_on=True, content="always"),
    ]
    messages = _messages("讲讲 io")

    out = readside.build_block(entries, messages)

    assert out["block"] == worldbook_match.build_world_book_block(
        [entries[0], entries[2]],
        messages,
    )
    assert out["matched_names"] == ["io项目", "wb3"]
    assert out["rejected_over_cap"] == []


def test_build_block_excludes_over_cap_entries_and_reports_them():
    huge = "x" * 11
    entries = [
        _entry("too-big", name="超长", keywords=["io"], content=huge),
        _entry("ok", name="正常", keywords=["io"], content="small"),
    ]

    out = readside.build_block(entries, _messages("io"), content_cap=10)

    assert "small" in out["block"]
    assert huge not in out["block"]
    assert out["matched_names"] == ["正常"]
    assert out["rejected_over_cap"] == ["too-big"]
