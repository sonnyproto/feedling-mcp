from __future__ import annotations

import sys
import types
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime import memory_tools  # noqa: E402


def test_memory_tool_instruction_includes_v1_context_framing():
    prompt = memory_tools.memory_tool_instruction_message()["content"]
    assert "Memory context framing" in prompt
    assert "Ambient memories are background color" in prompt


def test_memory_index_tool_calls_readside_core_and_records_trace(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_tools")
    trace: dict = {}
    calls = []

    def fake_index(store_arg, api_key, payload):
        calls.append((store_arg.user_id, api_key, dict(payload)))
        return {
            "items": [{"id": "mem_cat", "summary": "用户家猫叫武松。"}],
            "limit": 50,
            "user_card_count": 12,
        }

    monkeypatch.setattr(memory_tools.memory_readside_core, "memory_index_core", fake_index)

    result = memory_tools.execute_memory_tool(
        store,
        "key_tools",
        "memory_index",
        {"query": "猫叫什么", "limit": 80},
        trace=trace,
    )

    assert calls == [("usr_tools", "key_tools", {"query": "猫叫什么", "limit": 80, "include_sensitive": False})]
    assert result["ok"] is True
    assert result["name"] == "memory_index"
    assert result["items"][0]["id"] == "mem_cat"
    assert trace["mode"] == "agent_tools"
    assert trace["index_called"] is True
    assert trace["user_card_count"] == 12
    assert trace["tool_calls"][0]["name"] == "memory_index"
    assert trace["tool_calls"][0]["item_count"] == 1


def test_memory_index_tool_passes_bucket_thread_filters(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_tools")
    captured = {}

    def fake_index(store_arg, api_key, payload):
        captured["payload"] = dict(payload)
        return {"items": [], "limit": 1000, "user_card_count": 0}

    monkeypatch.setattr(memory_tools.memory_readside_core, "memory_index_core", fake_index)

    result = memory_tools.execute_memory_tool(
        store,
        "key_tools",
        "memory_index",
        {"query": "蛋子", "bucket": "宠物", "thread": "蛋子"},
        trace={},
    )

    assert result["ok"] is True
    assert captured["payload"]["bucket"] == "宠物"
    assert captured["payload"]["thread"] == "蛋子"


def test_memory_fetch_tool_caps_dedupes_and_records_trace(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_tools")
    trace = {"fetched_ids": ["already"], "tool_calls": [], "cumulative_fetch_limit": 8}
    captured = {}

    def fake_fetch(store_arg, api_key, payload):
        captured["payload"] = dict(payload)
        return {
            "items": [{"id": mid, "summary": f"summary {mid}"} for mid in payload["ids"]],
            "missing_ids": [],
            "unavailable_ids": [],
        }

    monkeypatch.setattr(memory_tools.memory_readside_core, "memory_fetch_core", fake_fetch)

    result = memory_tools.execute_memory_tool(
        store,
        "key_tools",
        "memory_fetch",
        {"ids": ["a", "b", "a", "c", "d", "e", "f", "already", "g"]},
        trace=trace,
    )

    assert captured["payload"]["ids"] == ["a", "b", "c", "d", "e"]
    assert result["ok"] is True
    assert result["capped"] is True
    assert [item["id"] for item in result["items"]] == ["a", "b", "c", "d", "e"]
    assert trace["fetch_called"] is True
    assert trace["fetched_ids"] == ["already", "a", "b", "c", "d", "e"]
    assert trace["tool_calls"][-1]["name"] == "memory_fetch"
    assert trace["tool_calls"][-1]["ids"] == ["a", "b", "c", "d", "e"]
