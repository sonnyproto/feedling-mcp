from __future__ import annotations

import sys
import threading
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from hosted import context as hosted_context  # noqa: E402


def _store(world_books=None):
    return types.SimpleNamespace(
        user_id="usr_worldbook_inject",
        world_books=world_books or [{"id": "wb1", "body_ct": "ct"}],
        world_books_lock=threading.Lock(),
        frames_meta=[],
        frames_lock=threading.Lock(),
    )


def _fake_gate(path, _api_key, params=None):
    if path == "/v1/chat/history":
        return {
            "messages": [{"role": "user", "content": "之前聊过 io"}],
            "context_memories": [{"id": "mem1", "title": "semantic memory"}],
            "context_memory_trace": {},
        }, ""
    if path == "/v1/identity/get":
        return {"identity": {"agent_name": "Kai", "self_introduction": "我是 Kai"}}, ""
    return {}, ""


def test_worldbook_block_is_injected_as_separate_foreground_message(monkeypatch):
    monkeypatch.setattr(hosted_context.core_enclave, "_enclave_get_json_for_gate", _fake_gate)
    monkeypatch.setattr(hosted_context.hosted_turn, "_state_pending_items", lambda _store: [])
    monkeypatch.setattr(hosted_context, "_perception_wake_snapshot", lambda _uid: {})

    captured = {}

    def fake_worldbook(api_key, world_books, messages, *, runtime_token=None):
        captured["api_key"] = api_key
        captured["world_books"] = world_books
        captured["messages"] = messages
        return {"block": "<world_book>\n[io项目] io 是产品\n</world_book>", "matched_names": ["io项目"]}

    monkeypatch.setattr(hosted_context.worldbook_readside_core, "post_enclave_worldbook_match", fake_worldbook)

    messages, payload, _images = hosted_context._model_api_context_messages(
        _store(),
        "ak_worldbook",
        "现在讲讲 io",
        include_screen_context=False,
    )

    assert payload["world_book"]["matched_names"] == ["io项目"]
    assert captured["messages"][-1] == {"role": "user", "content": "现在讲讲 io"}
    contents = [m["content"] for m in messages]
    worldbook_idx = next(i for i, c in enumerate(contents) if "<world_book>" in c)
    runtime_idx = next(i for i, c in enumerate(contents) if c.startswith("Feedling runtime context JSON"))
    assert worldbook_idx == runtime_idx + 1
    assert "semantic memory" in contents[runtime_idx]
    assert "<world_book>" not in contents[runtime_idx]


def test_empty_worldbook_result_injects_nothing(monkeypatch):
    monkeypatch.setattr(hosted_context.core_enclave, "_enclave_get_json_for_gate", _fake_gate)
    monkeypatch.setattr(hosted_context.hosted_turn, "_state_pending_items", lambda _store: [])
    monkeypatch.setattr(hosted_context, "_perception_wake_snapshot", lambda _uid: {})
    monkeypatch.setattr(
        hosted_context.worldbook_readside_core,
        "post_enclave_worldbook_match",
        lambda *a, **k: {"block": "", "matched_names": []},
    )

    messages, payload, _images = hosted_context._model_api_context_messages(
        _store(),
        "ak_worldbook",
        "没有命中",
        include_screen_context=False,
    )

    assert payload["world_book"]["matched_names"] == []
    assert all("<world_book>" not in m["content"] for m in messages)
