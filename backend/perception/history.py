"""Quantitative perception history — incremental daily aggregation (Tier 2).

Field-agnostic by design (PERCEPTION_HISTORY_SPEC principle #2): each signal
declares ONE *shape* in ``SHAPE``; a per-shape merge function folds new
observations into the running daily doc. Adding a field flows through
automatically (numeric fields are discovered from the values dict); adding a
signal is one ``SHAPE`` line. There is no per-field list to keep in sync with
iOS — exactly so the ``temperature_bucket -> temperature`` class of rename
never breaks history again.

Everything here is a PURE function (no DB / no I/O): ``record_daily`` takes the
previous day-doc + a new observation and returns the next day-doc;
``read_trend`` derives a baseline/delta from a list of day-docs. Storage (the
``perception_daily`` table), the ingest hook, and the read endpoint wire these
in separately so the math is unit-testable without Postgres.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# --- shapes -----------------------------------------------------------------
NUMERIC_DIST = "numeric_dist"        # per numeric field: min/max/sum/count -> avg
CUMULATIVE = "cumulative"            # per numeric field: running max (= daily total)
MAIN_OF_DAY = "main_of_day"          # latest non-null point values (replace)
DURATION_BY_STATE = "duration_by_state"  # minutes spent in each categorical state
EVENT_LIST = "event_list"            # discrete items, deduped by id/key
SUBJECTIVE = "subjective"            # append each self-report entry
PLACE_DWELL = "place_dwell"          # minutes spent at each place label

# Signal (canonical catalog input key) -> shape. ONE line per signal; fields are
# discovered from the observation. Signals absent here are NOT historized
# (pure-instant / no daily pattern: time, battery, broadcast, now, app).
SHAPE: dict[str, str] = {
    "health_vitals": NUMERIC_DIST,
    "health_metabolic": NUMERIC_DIST,
    "weather": NUMERIC_DIST,
    "health_activity": CUMULATIVE,
    "health_sleep": MAIN_OF_DAY,
    "health_body": MAIN_OF_DAY,
    "health_cycle": MAIN_OF_DAY,
    "health_mood": SUBJECTIVE,
    "motion_state": DURATION_BY_STATE,
    "focus": DURATION_BY_STATE,
    "audio_route": DURATION_BY_STATE,
    "location_signal": PLACE_DWELL,
    "health_workout": EVENT_LIST,
    "calendar_next_event": EVENT_LIST,
    "reminders": EVENT_LIST,
}

# The single categorical field that names the "state" for duration/place shapes.
# (Field-agnostic everywhere else; these two shapes need to know which key is the
# state label vs. the timestamp accounting.)
_STATE_FIELD = {
    "motion_state": "motion_state",
    "focus": "in_focus",
    "audio_route": "output_type",
    "location_signal": "place_label",
}
# Fields that are pure-instant noise even inside a historized signal.
_SKIP_FIELDS = {"step_count"}  # cumulative dup of the `steps` signal; handled there


def is_historized(signal: str) -> bool:
    return signal in SHAPE


def _numeric(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _flatten_state(v: Any) -> str | None:
    """Coerce a state value to a categorical label (motion_state is a nested
    dict {state, confidence, ...}; focus in_focus is a bool)."""
    if isinstance(v, Mapping):
        s = v.get("state")
        return str(s) if s is not None else None
    if isinstance(v, bool):
        return "focused" if v else "unfocused"
    if v is None:
        return None
    return str(v)


# --- per-shape incremental merges ------------------------------------------
def _merge_numeric_dist(doc: dict, values: Mapping, **_) -> dict:
    out = dict(doc)
    for field, raw in values.items():
        if field in _SKIP_FIELDS:
            continue
        n = _numeric(raw)
        if n is None:
            continue
        cell = out.get(field) or {}
        out[field] = {
            "min": n if cell.get("min") is None else min(cell["min"], n),
            "max": n if cell.get("max") is None else max(cell["max"], n),
            "sum": (cell.get("sum") or 0.0) + n,
            "count": (cell.get("count") or 0) + 1,
        }
    return out


def _merge_cumulative(doc: dict, values: Mapping, **_) -> dict:
    out = dict(doc)
    for field, raw in values.items():
        n = _numeric(raw)
        if n is None:
            continue
        prev = out.get(field)
        out[field] = {"total": n if prev is None else max(prev.get("total", n), n)}
    return out


def _merge_main_of_day(doc: dict, values: Mapping, *, ts: float | None = None, **_) -> dict:
    out = dict(doc)
    for field, raw in values.items():
        if raw is not None:
            out[field] = raw
    if ts is not None:
        out["_at"] = ts
    return out


def _merge_duration_by_state(doc: dict, values: Mapping, *, signal: str, ts: float | None = None, **_) -> dict:
    out = dict(doc)
    buckets = dict(out.get("minutes") or {})
    state = _flatten_state(values.get(_STATE_FIELD.get(signal, "")))
    last_state = out.get("_last_state")
    last_ts = out.get("_last_ts")
    if last_state is not None and last_ts is not None and ts is not None and ts >= last_ts:
        mins = (ts - last_ts) / 60.0
        buckets[last_state] = round((buckets.get(last_state) or 0.0) + mins, 2)
    out["minutes"] = buckets
    if state is not None:
        out["_last_state"] = state
    if ts is not None:
        out["_last_ts"] = ts
    return out


def _merge_place_dwell(doc: dict, values: Mapping, *, ts: float | None = None, **_) -> dict:
    out = dict(doc)
    buckets = dict(out.get("minutes") or {})
    place = values.get("place_label")
    last_place = out.get("_last_place")
    last_ts = out.get("_last_ts")
    if last_place and last_ts is not None and ts is not None and ts >= last_ts:
        mins = (ts - last_ts) / 60.0
        buckets[last_place] = round((buckets.get(last_place) or 0.0) + mins, 2)
    out["minutes"] = buckets
    if place:
        out["_last_place"] = place
        visited = set(out.get("visited") or [])
        visited.add(place)
        out["visited"] = sorted(visited)
    if ts is not None:
        out["_last_ts"] = ts
    return out


def _event_key(ev: Mapping) -> str:
    for k in ("id", "event_id", "identifier"):
        if ev.get(k):
            return str(ev[k])
    return "|".join(str(ev.get(k) or "") for k in ("title", "next_event_time", "start_time", "due_time"))


def _merge_event_list(doc: dict, values: Mapping, **_) -> dict:
    out = dict(doc)
    items = list(out.get("events") or [])
    seen = {_event_key(e) for e in items if isinstance(e, Mapping)}
    # event-list signals carry their events under a list-valued field (e.g.
    # calendar_events / reminders); fall back to the values dict as one event.
    candidates: list = []
    for v in values.values():
        if isinstance(v, list):
            candidates.extend(x for x in v if isinstance(x, Mapping))
    if not candidates and any(values.get(k) for k in ("title", "workout_type")):
        candidates = [dict(values)]
    for ev in candidates:
        key = _event_key(ev)
        if key and key not in seen:
            seen.add(key)
            items.append(ev)
    out["events"] = items
    return out


def _merge_subjective(doc: dict, values: Mapping, *, ts: float | None = None, **_) -> dict:
    out = dict(doc)
    entries = list(out.get("entries") or [])
    entry = {k: v for k, v in values.items() if v is not None}
    if ts is not None:
        entry["_at"] = ts
    if entry:
        entries.append(entry)
    out["entries"] = entries
    return out


_MERGERS = {
    NUMERIC_DIST: _merge_numeric_dist,
    CUMULATIVE: _merge_cumulative,
    MAIN_OF_DAY: _merge_main_of_day,
    DURATION_BY_STATE: _merge_duration_by_state,
    PLACE_DWELL: _merge_place_dwell,
    EVENT_LIST: _merge_event_list,
    SUBJECTIVE: _merge_subjective,
}


def record_daily(prev_doc: Mapping | None, signal: str, values: Mapping, *, ts: float | None = None) -> dict:
    """Fold one observation of ``signal`` into its running day-doc and return the
    next day-doc. Caller is responsible for keying by (user, local-date, signal)
    and for resetting prev_doc to {} when the local date rolls over."""
    shape = SHAPE.get(signal)
    if shape is None:
        raise ValueError(f"signal {signal!r} is not historized; guard with is_historized()")
    if not isinstance(values, Mapping):
        return dict(prev_doc or {})
    merge = _MERGERS[shape]
    return merge(dict(prev_doc or {}), values, signal=signal, ts=ts)


# --- read side: trend / baseline -------------------------------------------
def _series_value(doc: Mapping, shape: str, field: str | None) -> float | None:
    """Pull a single comparable daily number out of a day-doc for trending."""
    if shape == NUMERIC_DIST:
        cell = doc.get(field) if field else None
        if isinstance(cell, Mapping) and cell.get("count"):
            return round(cell["sum"] / cell["count"], 3)
        return None
    if shape == CUMULATIVE:
        cell = doc.get(field) if field else None
        return cell.get("total") if isinstance(cell, Mapping) else None
    if shape == MAIN_OF_DAY:
        v = doc.get(field) if field else None
        return _numeric(v)
    return None


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _percentile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    idx = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[idx]


def read_trend(rows: list[Mapping], signal: str, field: str | None = None) -> dict:
    """rows: [{date, doc}] ascending by date. Returns daily series + rolling
    baseline (median/p25/p75) + current + delta vs baseline + direction."""
    shape = SHAPE.get(signal)
    daily = []
    for r in rows:
        v = _series_value(r.get("doc") or {}, shape, field) if shape else None
        if v is not None:
            daily.append({"date": r.get("date"), "value": v})
    vals = [d["value"] for d in daily]
    baseline_vals = vals[:-1] if len(vals) > 1 else vals
    median = _median(baseline_vals)
    current = vals[-1] if vals else None
    delta = round(current - median, 3) if (current is not None and median is not None) else None
    direction = "flat"
    if delta is not None:
        direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")
    return {
        "signal": signal,
        "field": field,
        "daily": daily,
        "baseline": {
            "median": median,
            "p25": _percentile(baseline_vals, 0.25),
            "p75": _percentile(baseline_vals, 0.75),
            "n": len(baseline_vals),
        },
        "current": current,
        "delta": delta,
        "direction": direction,
    }
