from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import mcp_server  # noqa: E402


def test_memory_index_returns_items_guidance_and_selector_trace(monkeypatch):
    calls = []

    def fake_post(path, body, ctx=None):
        calls.append((path, body))
        assert path == "/v1/memory/index"
        return {
            "items": [
                {
                    "id": "mem_cat",
                    "summary": "用户担心猫咪生病时，需要先共情再给具体观察建议。",
                    "bucket_refs": ["猫咪", "宠物照顾"],
                    "status": "active",
                    "salience": "high",
                    "is_open_thread": True,
                    "is_sensitive": False,
                    "score": 0.91,
                },
                {
                    "id": "mem_private",
                    "summary": "用户有一条私密边界记忆。",
                    "bucket_refs": ["亲密边界"],
                    "status": "active",
                    "salience": "high",
                    "is_sensitive": True,
                    "score": 0.88,
                },
            ]
        }

    monkeypatch.setattr(mcp_server, "_post", fake_post)

    out = mcp_server.memory_index(query="猫咪最近不吃饭，我有点担心", limit=100)

    assert calls == [("/v1/memory/index", {"limit": 50, "include_sensitive": False})]
    assert out["recall_flow"] == "index_first_fetch_later"
    assert "feedling_memory_fetch" in out["guidance"]
    assert out["suggested_ids"] == ["mem_cat"]
    assert out["selector_trace"]["selected"][0]["id"] == "mem_cat"
    skipped = out["selector_trace"]["skipped_sample"]
    assert any(item["id"] == "mem_private" and item["reason"] == "sensitive_not_allowed_for_query" for item in skipped)


def test_memory_index_without_query_does_not_run_selector(monkeypatch):
    monkeypatch.setattr(
        mcp_server,
        "_post",
        lambda path, body, ctx=None: {"items": [{"id": "mem_1", "summary": "一条摘要"}]},
    )

    out = mcp_server.memory_index(query="", limit=5)

    assert out["items"] == [{"id": "mem_1", "summary": "一条摘要"}]
    assert "suggested_ids" not in out
    assert "selector_trace" not in out


def test_memory_fetch_dedupes_ids_and_forwards_flags(monkeypatch):
    calls = []

    def fake_post(path, body, ctx=None):
        calls.append((path, body))
        return {
            "items": [{"id": "mem_2"}, {"id": "mem_1"}],
            "missing_ids": [],
            "unavailable_ids": [],
        }

    monkeypatch.setattr(mcp_server, "_post", fake_post)

    out = mcp_server.memory_fetch(
        ids=["mem_2", "", "mem_1", "mem_2"],
        include_archived=True,
        include_superseded=True,
    )

    assert calls == [
        (
            "/v1/memory/fetch",
            {
                "ids": ["mem_2", "mem_1"],
                "include_archived": True,
                "include_superseded": True,
            },
        )
    ]
    assert [item["id"] for item in out["items"]] == ["mem_2", "mem_1"]


def test_memory_fetch_requires_ids():
    out = mcp_server.memory_fetch(ids=[])

    assert out["error"] == "ids_required"
    assert out["items"] == []
