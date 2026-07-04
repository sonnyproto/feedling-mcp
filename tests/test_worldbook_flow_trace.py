from __future__ import annotations

import sys
import threading
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from hosted import context as hosted_context  # noqa: E402


def _store():
    return types.SimpleNamespace(
        user_id="usr_worldbook_trace",
        world_books=[{"id": "wb1", "body_ct": "ct"}],
        world_books_lock=threading.Lock(),
        frames_meta=[],
        frames_lock=threading.Lock(),
    )


def _fake_gate(path, _api_key, params=None):
    if path == "/v1/chat/history":
        return {"messages": [], "context_memories": [], "context_memory_trace": {}}, ""
    if path == "/v1/identity/get":
        return {"identity": {"agent_name": "Kai"}}, ""
    return {}, ""


def _install_worldbook(monkeypatch):
    monkeypatch.setattr(hosted_context.core_enclave, "_enclave_get_json_for_gate", _fake_gate)
    monkeypatch.setattr(hosted_context.hosted_turn, "_state_pending_items", lambda _store: [])
    monkeypatch.setattr(hosted_context, "_perception_wake_snapshot", lambda _uid: {})
    monkeypatch.setattr(
        hosted_context.worldbook_readside_core,
        "post_enclave_worldbook_match",
        lambda *a, **k: {
            "block": "<world_book>\n[io项目] io 是产品\n</world_book>",
            "matched_names": ["io项目"],
        },
    )


def test_worldbook_injected_trace_emits_when_gate_enabled(monkeypatch):
    _install_worldbook(monkeypatch)
    events = []
    monkeypatch.setattr(hosted_context.debug_trace, "is_enabled", lambda _store: True)
    monkeypatch.setattr(hosted_context.debug_trace, "trace_event", lambda *a, **k: events.append(k))

    hosted_context._model_api_context_messages(
        _store(),
        "ak_worldbook",
        "io",
        include_screen_context=False,
    )

    assert events
    assert events[0]["type"] == "worldbook_injected"
    assert events[0]["detail"] == {"names": ["io项目"]}


def test_worldbook_injected_trace_skips_when_gate_disabled(monkeypatch):
    _install_worldbook(monkeypatch)
    events = []
    monkeypatch.setattr(hosted_context.debug_trace, "is_enabled", lambda _store: False)
    monkeypatch.setattr(hosted_context.debug_trace, "trace_event", lambda *a, **k: events.append(k))

    hosted_context._model_api_context_messages(
        _store(),
        "ak_worldbook",
        "io",
        include_screen_context=False,
    )

    assert events == []
