"""Unit tests for the cross-worker wake bus dispatch (core/wake_bus.py).

Covers the routing logic only — no Postgres: notify()'s SQL is monkeypatched and
the store-channel path is exercised against an uncached user (so _evict_store
returns without a DB read). The real two-worker LISTEN/NOTIFY round trip is a
Step-5 integration concern.

Run:  python -m pytest tests/test_wake_bus.py -q
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from core import wake_bus


def _reset_handlers():
    wake_bus._extra_handlers.clear()


def test_notify_payload_shape(monkeypatch):
    captured = {}

    def fake_pg_notify(channel, payload):
        captured["channel"] = channel
        captured["payload"] = json.loads(payload)

    monkeypatch.setattr(wake_bus.db, "pg_notify", fake_pg_notify)
    monkeypatch.setenv("FEEDLING_WAKE_BUS_ENABLED", "1")
    wake_bus.notify("chat", "user-42")

    assert captured["channel"] == wake_bus.PG_CHANNEL
    assert captured["payload"] == {"u": "user-42", "c": "chat", "o": wake_bus.WORKER_ID}


def test_notify_disabled_is_noop(monkeypatch):
    called = []
    monkeypatch.setattr(wake_bus.db, "pg_notify", lambda *a, **k: called.append(a))
    monkeypatch.setenv("FEEDLING_WAKE_BUS_ENABLED", "0")
    wake_bus.notify("chat", "user-42")
    assert called == []


def test_dispatch_skips_self_origin(monkeypatch):
    _reset_handlers()
    fired = []
    wake_bus.register_handler("chat", lambda uid: fired.append(uid))
    wake_bus._dispatch(json.dumps({"u": "u1", "c": "chat", "o": wake_bus.WORKER_ID}))
    assert fired == []  # our own write — handlers must not run
    _reset_handlers()


def test_dispatch_runs_injected_handler_for_other_worker(monkeypatch):
    _reset_handlers()
    fired = []
    wake_bus.register_handler("users", lambda uid: fired.append(uid))
    wake_bus._dispatch(json.dumps({"u": "u9", "c": "users", "o": "OTHER_WORKER"}))
    assert fired == ["u9"]
    _reset_handlers()


def test_dispatch_store_channel_evicts(monkeypatch):
    # Cross-origin store-channel notify must call _evict_store for the user.
    from core import store as core_store

    seen = []
    monkeypatch.setattr(core_store, "_evict_store", lambda uid: seen.append(uid))
    wake_bus._dispatch(json.dumps({"u": "u7", "c": "proactive", "o": "OTHER"}))
    assert seen == ["u7"]


def test_dispatch_ignores_malformed_payload():
    wake_bus._dispatch("not json")  # must not raise
