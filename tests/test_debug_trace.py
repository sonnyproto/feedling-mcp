"""v1 flow trace: beta default-on recording with deploy/per-user safety valves."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import db  # noqa: E402
import debug_trace  # noqa: E402


class _Store:
    def __init__(self, uid="usr_dbg"):
        self.user_id = uid


def _reset(monkeypatch, store):
    debug_trace._flag_cache.clear()
    # isolate DB blobs to an in-memory dict so the test never needs Postgres
    blobs: dict = {}
    monkeypatch.setattr(db, "get_blob", lambda uid, kind: blobs.get((uid, kind)))
    monkeypatch.setattr(db, "set_blob", lambda uid, kind, doc: blobs.__setitem__((uid, kind), doc))
    return blobs


def test_default_on_records_no_env_needed(monkeypatch):
    store = _Store()
    blobs = _reset(monkeypatch, store)
    monkeypatch.delenv("FEEDLING_V1_FLOW_TRACE", raising=False)  # no env set (default)
    debug_trace.trace_event(store, subsystem="route", type="route.decided", summary="x")
    assert debug_trace.is_enabled(store) is True
    assert debug_trace.read_trace(store)[0]["type"] == "route.decided"
    assert (store.user_id, debug_trace.DEBUG_TRACE_BLOB) in blobs


def test_default_can_be_restored_to_opt_in_with_env(monkeypatch):
    store = _Store()
    blobs = _reset(monkeypatch, store)
    monkeypatch.delenv("FEEDLING_V1_FLOW_TRACE", raising=False)
    monkeypatch.setenv("FEEDLING_V1_FLOW_TRACE_DEFAULT", "0")
    debug_trace.trace_event(store, subsystem="route", type="route.decided", summary="x")
    assert debug_trace.is_enabled(store) is False
    assert debug_trace.read_trace(store) == []
    assert (store.user_id, debug_trace.DEBUG_TRACE_BLOB) not in blobs
    debug_trace.set_enabled(store, True)
    debug_trace.trace_event(store, subsystem="route", type="route.decided", summary="x")
    assert debug_trace.read_trace(store)[0]["type"] == "route.decided"


def test_env_zero_hard_disables_even_with_flag_on(monkeypatch):
    store = _Store()
    _reset(monkeypatch, store)
    monkeypatch.setenv("FEEDLING_V1_FLOW_TRACE", "0")  # prod kill switch
    debug_trace.set_enabled(store, True)  # user toggled on, but...
    debug_trace.trace_event(store, subsystem="route", type="route.decided")
    assert debug_trace.is_enabled(store) is False
    assert debug_trace.read_trace(store) == []


def test_records_when_flag_on_no_env_needed(monkeypatch):
    store = _Store()
    _reset(monkeypatch, store)
    monkeypatch.delenv("FEEDLING_V1_FLOW_TRACE", raising=False)
    debug_trace.trace_event(store, subsystem="route", type="route.decided",
                            summary="host", detail={"mode": "agent_runtime", "reason": "text"})
    debug_trace.trace_event(store, subsystem="memory", type="memory.index.called",
                            detail={"counts": {"items": 50, "fetched": 2}})
    events = debug_trace.read_trace(store)
    assert [e["type"] for e in events] == ["memory.index.called", "route.decided"]  # newest first
    assert debug_trace.read_trace(store, subsystem="route")[0]["detail"]["mode"] == "agent_runtime"
    # Per-user opt-out still works.
    debug_trace.set_enabled(store, False)
    debug_trace.trace_event(store, subsystem="route", type="route.decided")
    assert len(debug_trace.read_trace(store)) == 2


def test_detail_is_size_bounded_metadata(monkeypatch):
    store = _Store()
    _reset(monkeypatch, store)
    monkeypatch.setenv("FEEDLING_V1_FLOW_TRACE", "1")
    debug_trace.set_enabled(store, True)
    debug_trace.trace_event(store, subsystem="memory", type="t", detail={"big": "x" * 9999})
    ev = debug_trace.read_trace(store)[0]
    assert len(ev["detail"]["big"]) <= 200  # caller content can't bloat the buffer


# --- M1: explain / content_excerpt / dur_ms + verbose gate + caps -----------


class FakeStore:
    def __init__(self, uid="u1"):
        self.user_id = uid


def _reset_verbose(monkeypatch):
    """In-memory blob store + force gate ON."""
    blobs = {}
    monkeypatch.setattr(debug_trace.db, "get_blob", lambda uid, k: blobs.get((uid, k)))
    monkeypatch.setattr(debug_trace.db, "set_blob", lambda uid, k, v: blobs.__setitem__((uid, k), v))
    monkeypatch.setattr(debug_trace, "_hard_disabled", lambda: False)
    debug_trace._flag_cache.clear()
    return blobs


def test_verbose_off_strips_content_excerpt(monkeypatch):
    _reset_verbose(monkeypatch)
    store = FakeStore()
    debug_trace.set_enabled(store, True)
    monkeypatch.setenv("FEEDLING_DEBUG_VERBOSE", "0")  # force strip
    debug_trace.trace_event(store, subsystem="agent", type="agent.model.call.done",
                            explain="模型返回", content_excerpt={"reply": "hello"}, dur_ms=12.0)
    ev = debug_trace.read_trace(store, limit=10)[0]
    assert ev["explain"] == "模型返回"
    assert ev["dur_ms"] == 12.0
    assert ev.get("content_excerpt") in (None, {}, )  # stripped when verbose off


def test_content_excerpt_field_truncation(monkeypatch):
    _reset_verbose(monkeypatch)
    store = FakeStore()
    debug_trace.set_enabled(store, True)
    monkeypatch.delenv("FEEDLING_DEBUG_VERBOSE", raising=False)  # verbose defaults ON with gate
    big = "x" * 5000
    debug_trace.trace_event(store, subsystem="agent", type="t",
                            content_excerpt={"prompt": big})
    ev = debug_trace.read_trace(store, limit=10)[0]
    assert len(ev["content_excerpt"]["prompt"]) <= 2048 + len("…(truncated)")
    assert ev["content_excerpt"]["prompt"].endswith("…(truncated)")


def test_verbose_ring_cap(monkeypatch):
    _reset_verbose(monkeypatch)
    store = FakeStore()
    debug_trace.set_enabled(store, True)
    monkeypatch.delenv("FEEDLING_DEBUG_VERBOSE", raising=False)
    for i in range(260):
        debug_trace.trace_event(store, subsystem="route", type=f"t{i}")
    assert len(debug_trace.read_trace(store, limit=1000)) == 200  # verbose cap
