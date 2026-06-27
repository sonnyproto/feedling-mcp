"""v1 flow trace — gated per-user observability ring buffer (M0).

Turns "I feel like the flow ran" into "I did X, the panel shows event Y, so the
path objectively ran." Covers host(agent_runtime) + vps(resident consumer) +
genesis/memory/identity/perception/proactive because the events are recorded
where the flow actually happens (backend turn internals + incoming tool calls +
the resident consumer).

PRIVACY: the panel is a flow indicator, NOT a plaintext log. Callers must pass
METADATA ONLY in `detail` — ids, counts, route reasons, status, source_kind,
persona_version hash. Never raw memory content / transcript / persona text. The
whole thing is a no-op unless the per-user flag is on (off by default).
"""
from __future__ import annotations

import os
import time
from typing import Any

import db

DEBUG_TRACE_BLOB = "v1_flow_trace"
DEBUG_TRACE_FLAG_BLOB = "v1_flow_trace_enabled"
_MAX_EVENTS = 500
_TTL_SEC = 24 * 3600
_FLAG_CACHE_TTL = 30.0

# subsystem ∈ memory|genesis|identity|route|voice|perception|proactive|fallback|account
# actor     ∈ host_agent_runtime|vps_resident|backend|ios

_flag_cache: dict[str, tuple[bool, float]] = {}


def _deploy_enabled() -> bool:
    """Deploy-level kill switch. OFF by default → the whole feature is a pure
    no-op in production (not even a DB read). Turn ON only on the test deploy;
    then the per-user debug-panel flag actually controls recording."""
    return os.environ.get("FEEDLING_V1_FLOW_TRACE", "").strip().lower() in ("1", "true", "yes", "on")


def is_enabled(store) -> bool:
    if not _deploy_enabled():
        return False
    uid = getattr(store, "user_id", "") or ""
    if not uid:
        return False
    now = time.time()
    cached = _flag_cache.get(uid)
    if cached and cached[1] > now:
        return cached[0]
    flag = db.get_blob(uid, DEBUG_TRACE_FLAG_BLOB) or {}
    enabled = bool(flag.get("enabled")) if isinstance(flag, dict) else bool(flag)
    _flag_cache[uid] = (enabled, now + _FLAG_CACHE_TTL)
    return enabled


def set_enabled(store, enabled: bool) -> dict:
    uid = getattr(store, "user_id", "") or ""
    doc = {"enabled": bool(enabled), "updated_at": time.time()}
    if uid:
        db.set_blob(uid, DEBUG_TRACE_FLAG_BLOB, doc)
        _flag_cache[uid] = (bool(enabled), time.time() + _FLAG_CACHE_TTL)
    return doc


def trace_event(
    store,
    *,
    subsystem: str,
    type: str,
    summary: str = "",
    detail: dict[str, Any] | None = None,
    actor: str = "backend",
    status: str = "ok",
    trace_id: str = "",
    turn_id: str = "",
    job_id: str = "",
) -> None:
    """Append one flow event to the per-user ring buffer — no-op unless enabled.

    Best-effort: never raises (debug must not break the request path)."""
    try:
        if not is_enabled(store):
            return
        uid = getattr(store, "user_id", "") or ""
        if not uid:
            return
        now = time.time()
        event = {
            "ts": now,
            "subsystem": str(subsystem or "")[:40],
            "type": str(type or "")[:80],
            "actor": str(actor or "backend")[:40],
            "status": str(status or "ok")[:20],
            "summary": str(summary or "")[:300],
            "trace_id": str(trace_id or "")[:120],
            "turn_id": str(turn_id or "")[:120],
            "job_id": str(job_id or "")[:120],
            "detail": _safe_detail(detail),
        }
        buf = db.get_blob(uid, DEBUG_TRACE_BLOB)
        events = buf.get("events") if isinstance(buf, dict) and isinstance(buf.get("events"), list) else []
        events.append(event)
        # drop TTL-expired + cap to the most recent _MAX_EVENTS
        cutoff = now - _TTL_SEC
        events = [e for e in events if float(e.get("ts") or 0) >= cutoff][-_MAX_EVENTS:]
        db.set_blob(uid, DEBUG_TRACE_BLOB, {"v": 1, "events": events})
    except Exception:
        pass  # observability must never break the actual flow


def read_trace(store, *, limit: int = 200, subsystem: str = "") -> list[dict]:
    uid = getattr(store, "user_id", "") or ""
    if not uid:
        return []
    buf = db.get_blob(uid, DEBUG_TRACE_BLOB) or {}
    events = buf.get("events") if isinstance(buf, dict) and isinstance(buf.get("events"), list) else []
    if subsystem:
        events = [e for e in events if str(e.get("subsystem") or "") == subsystem]
    events = sorted(events, key=lambda e: float(e.get("ts") or 0), reverse=True)
    return events[: max(1, min(int(limit or 200), _MAX_EVENTS))]


def clear_trace(store) -> None:
    uid = getattr(store, "user_id", "") or ""
    if uid:
        db.set_blob(uid, DEBUG_TRACE_BLOB, {"v": 1, "events": []})


def _safe_detail(detail: dict[str, Any] | None) -> dict[str, Any]:
    """Shallow, size-bounded copy. Detail should already be metadata-only (ids/
    counts/reasons); this just bounds it so a careless caller can't bloat the buf."""
    if not isinstance(detail, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in list(detail.items())[:20]:
        key = str(k)[:40]
        if isinstance(v, (int, float, bool)):
            out[key] = v
        elif isinstance(v, str):
            out[key] = v[:200]
        elif isinstance(v, list):
            out[key] = [str(x)[:80] for x in v[:20]]
        elif isinstance(v, dict):
            out[key] = {str(kk)[:40]: (vv if isinstance(vv, (int, float, bool)) else str(vv)[:80]) for kk, vv in list(v.items())[:20]}
        else:
            out[key] = str(v)[:80]
    return out
