from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import worldbook_match as wb  # noqa: E402


def _entry(eid, *, name="", keywords=None, content="c", enabled=True, always=False):
    return {
        "id": eid,
        "name": name,
        "keywords": keywords or [],
        "content": content,
        "enabled": enabled,
        "alwaysOn": always,
    }


def _msgs(*texts):
    return [{"role": "user", "content": t} for t in texts]


def test_keyword_hit_injects():
    e = _entry("1", name="io项目", keywords=["io"], content="io is a product")
    block = wb.build_world_book_block([e], _msgs("讲讲 io 这个项目"))
    assert block == "<world_book>\n[io项目] io is a product\n</world_book>"


def test_no_hit_returns_empty():
    e = _entry("1", keywords=["io"], content="x")
    assert wb.build_world_book_block([e], _msgs("今天天气")) == ""


def test_case_insensitive():
    e = _entry("1", keywords=["IO"], content="x")
    assert wb.build_world_book_block([e], _msgs("about io stuff")) != ""


def test_always_on_injects_without_keyword():
    e = _entry("1", keywords=[], content="always", always=True)
    assert "always" in wb.build_world_book_block([e], _msgs("anything"))


def test_disabled_skipped():
    e = _entry("1", keywords=["io"], content="x", enabled=False)
    assert wb.build_world_book_block([e], _msgs("io")) == ""


def test_entry_injected_once_even_if_multiple_keywords_hit():
    e = _entry("1", keywords=["io", "project"], content="dup")
    block = wb.build_world_book_block([e], _msgs("io project"))
    assert block.count("dup") == 1


def test_scan_window_only_last_n():
    e = _entry("1", keywords=["io"], content="x")
    # "io" is 6 messages back; with n=5 it must fall out of the window
    msgs = _msgs("io", "a", "b", "c", "d", "e")
    assert wb.build_world_book_block([e], msgs, n=5) == ""


def test_merge_in_list_order():
    e1 = _entry("1", name="A", keywords=["k"], content="first")
    e2 = _entry("2", name="B", keywords=["k"], content="second")
    block = wb.build_world_book_block([e1, e2], _msgs("k"))
    assert block == "<world_book>\n[A] first\n[B] second\n</world_book>"


def test_long_content_not_truncated():
    big = "x" * 50000
    e = _entry("1", keywords=["io"], content=big)
    block = wb.build_world_book_block([e], _msgs("io"))
    assert big in block  # full content, no injection-time cap


def test_empty_content_entry_skipped():
    e = _entry("1", keywords=["io"], content="   ")
    assert wb.build_world_book_block([e], _msgs("io")) == ""
