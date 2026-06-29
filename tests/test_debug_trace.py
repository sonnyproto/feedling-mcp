"""v1 flow trace: prove it's a true no-op in production (deploy switch off) and
only records when BOTH the deploy switch and the per-user debug flag are on."""
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


def test_flag_off_is_noop_no_env_needed(monkeypatch):
    store = _Store()
    blobs = _reset(monkeypatch, store)
    monkeypatch.delenv("FEEDLING_V1_FLOW_TRACE", raising=False)  # no env set (default)
    # per-user toggle is the gate: off → nothing records, nothing written
    debug_trace.trace_event(store, subsystem="route", type="route.decided", summary="x")
    assert debug_trace.is_enabled(store) is False
    assert debug_trace.read_trace(store) == []
    assert (store.user_id, debug_trace.DEBUG_TRACE_BLOB) not in blobs


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
    monkeypatch.delenv("FEEDLING_V1_FLOW_TRACE", raising=False)  # no env — toggle is the gate
    # flag off → no-op
    debug_trace.trace_event(store, subsystem="route", type="route.decided")
    assert debug_trace.read_trace(store) == []
    # user opens the debug panel toggle → now it records (no env/redeploy needed)
    debug_trace.set_enabled(store, True)
    debug_trace.trace_event(store, subsystem="route", type="route.decided",
                            summary="host", detail={"mode": "agent_runtime", "reason": "text"})
    debug_trace.trace_event(store, subsystem="memory", type="memory.index.called",
                            detail={"counts": {"items": 50, "fetched": 2}})
    events = debug_trace.read_trace(store)
    assert [e["type"] for e in events] == ["memory.index.called", "route.decided"]  # newest first
    assert debug_trace.read_trace(store, subsystem="route")[0]["detail"]["mode"] == "agent_runtime"
    # turn it back off → no new events
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
