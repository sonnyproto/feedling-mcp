"""Agent-facing HTTP verbs used by the resident io_cli tool."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flask import Blueprint, jsonify, request

from accounts import auth
from perception import history as perception_history
from perception import service as perception_service
from perception import store as perception_store

bp = Blueprint("agent", __name__)

FAST_AGENT_PERCEPTION_SIGNALS = ("now", "location", "weather", "motion", "calendar")
SLOW_AGENT_PERCEPTION_SIGNALS = (
    "steps", "sleep", "workout", "vitals",
    "activity", "body", "metabolic", "cycle", "mood", "reminders",
)
PULL_ONLY_AGENT_PERCEPTION_SIGNALS = ("focus", "audio_route")
AGENT_PERCEPTION_SIGNALS = (
    FAST_AGENT_PERCEPTION_SIGNALS
    + SLOW_AGENT_PERCEPTION_SIGNALS
    + PULL_ONLY_AGENT_PERCEPTION_SIGNALS
)

_SIGNAL_FIELDS: dict[str, tuple[str, ...]] = {
    "now": (
        "local_time",
        "timezone",
        "locale",
        "battery_level",
        "charging",
        "place_label",
        "motion_state",
        "now_playing",
        "broadcast_state",
        "broadcast_active",
    ),
    "location": ("place_label", "wifi_label", "country", "locality", "wifi_anchor_id"),
    "weather": (
        "condition", "temperature", "apparent_temperature", "humidity",
        "precipitation_chance", "uv_index", "is_daylight", "alerts",
    ),
    "motion": ("motion_state",),
    "calendar": ("calendar_next_event", "calendar_events", "calendar_events_truncated"),
    "focus": ("focus_authorization_status", "in_focus"),
    "audio_route": ("output_type", "is_bluetooth", "device_name"),
    "steps": ("step_count",),
    "sleep": ("asleep_minutes", "core_minutes", "deep_minutes", "rem_minutes"),
    "workout": ("workout_type", "duration_min", "count_today"),
    "vitals": (
        "resting_heart_rate", "step_count", "current_heart_rate", "hrv_sdnn_ms",
        "respiratory_rate", "oxygen_saturation_pct", "vo2_max",
    ),
    "activity": ("active_energy_kcal", "exercise_minutes", "stand_minutes", "mindful_minutes"),
    "body": ("weight_kg", "bmi", "body_fat_pct", "height_cm"),
    "metabolic": ("blood_glucose_mmol_l", "blood_pressure_systolic", "blood_pressure_diastolic"),
    "cycle": ("flow_level", "is_active_period"),
    "mood": ("valence", "valence_classification", "kind", "label_count", "recorded_today"),
    "reminders": ("next_reminder", "reminders", "overdue_count", "due_today_count", "reminders_truncated"),
}

_SIGNAL_PERMISSION_KEYS: dict[str, tuple[str, ...]] = {
    "now": ("now", "time", "device", "battery", "broadcast"),
    "location": ("location", "location_signal"),
    "weather": ("weather",),
    "motion": ("motion", "motion_state"),
    "calendar": ("calendar", "calendar_next_event"),
    "focus": ("focus",),
    "audio_route": ("audio_route",),
    "steps": ("steps", "health", "health_vitals"),
    "sleep": ("sleep", "health", "health_sleep"),
    "workout": ("workout", "health", "health_workout"),
    "vitals": ("vitals", "health", "health_vitals"),
    "activity": ("activity", "health", "health_activity"),
    "body": ("body", "health", "health_body"),
    "metabolic": ("metabolic", "health", "health_metabolic"),
    "cycle": ("cycle", "health", "health_cycle"),
    "mood": ("mood", "health", "health_mood"),
    "reminders": ("reminders",),
}

_OFF_VALUES = {"0", "false", "off", "disabled", "switch_off", "switch-off", "no"}
_DENIED_VALUES = {
    "denied",
    "not_permitted",
    "not-permitted",
    "not_allowed",
    "not-allowed",
    "not_authorized",
    "not-authorized",
    "unauthorized",
    "restricted",
    "permission_denied",
}
_ALLOW_VALUES = {"1", "true", "on", "enabled", "allowed", "authorized", "granted", "yes"}


def _parse_signals(raw: str | None) -> list[str]:
    if not raw:
        return list(FAST_AGENT_PERCEPTION_SIGNALS)
    out: list[str] = []
    for part in str(raw or "").split(","):
        signal = part.strip().lower()
        if signal and signal not in out:
            out.append(signal)
    return out or list(FAST_AGENT_PERCEPTION_SIGNALS)


def _disabled(reason: str) -> dict[str, Any]:
    return {"disabled": True, "reason": reason}


def _boolish_doc_reason(value: Any) -> str:
    if isinstance(value, bool):
        return "" if value else "switch_off"
    if isinstance(value, (int, float)):
        return "" if bool(value) else "switch_off"
    normalized = str(value or "").strip().lower()
    if not normalized or normalized in _ALLOW_VALUES:
        return ""
    if normalized in _OFF_VALUES:
        return "switch_off"
    if normalized in _DENIED_VALUES:
        return "not_permitted"
    return ""


def _permission_state_reason(value: Any) -> str:
    if isinstance(value, Mapping):
        explicit_reason = str(value.get("reason") or "").strip().lower()
        for key in ("enabled", "allowed", "authorized", "granted", "permitted"):
            if key in value:
                reason = _boolish_doc_reason(value.get(key))
                if reason:
                    return "not_permitted" if explicit_reason in _DENIED_VALUES else reason
                return ""
        for key in ("state", "status", "permission", "value"):
            if key in value:
                reason = _boolish_doc_reason(value.get(key))
                if reason:
                    return reason
        return _boolish_doc_reason(explicit_reason)
    return _boolish_doc_reason(value)


def _permission_states_reason(settings: Mapping[str, Any], signal: str) -> str:
    states = settings.get("permission_states") if isinstance(settings, Mapping) else {}
    if not isinstance(states, Mapping):
        return ""
    for key in _SIGNAL_PERMISSION_KEYS.get(signal, (signal,)):
        if key not in states:
            continue
        reason = _permission_state_reason(states.get(key))
        if reason:
            return reason
    return ""


def _null_state_message_reason(state: Mapping[str, Any], signal: str) -> str:
    fields = _SIGNAL_FIELDS.get(signal, ())
    messages: list[str] = []
    for field in fields:
        cell = state.get(field) if isinstance(state, Mapping) else None
        if isinstance(cell, Mapping) and cell.get("v") is None and cell.get("msg"):
            messages.append(str(cell.get("msg") or ""))
    if not messages:
        return ""
    joined = " ".join(messages).lower()
    if any(marker in joined for marker in ("关闭", "已关", "switch off", "switched off", "disabled")):
        return "switch_off"
    if any(marker in joined for marker in ("未授权", "无授权", "not authorized", "not permitted", "permission")):
        return "not_permitted"
    if signal == "weather" and "weatherkit" in joined and "不可用" in joined:
        return "not_permitted"
    if signal in {"steps", "sleep", "workout", "vitals"} and "healthkit" in joined and "不可用" in joined:
        return "not_permitted"
    return ""


def _signal_doc(signal: str, snapshot: Mapping[str, Any], pull_snapshot: Mapping[str, Any]) -> dict[str, Any]:
    source = snapshot if signal == "now" else pull_snapshot
    if signal == "now":
        out = {field: source.get(field) for field in _SIGNAL_FIELDS[signal]}
        out["time"] = source.get("local_time")
        if "user_state" in source:
            out["user_state"] = source.get("user_state")
        return out
    return {field: source.get(field) for field in _SIGNAL_FIELDS[signal]}


@bp.route("/v1/agent/perception", methods=["GET"])
def agent_perception():
    user_store = auth.require_user()
    signals = _parse_signals(request.args.get("signals"))
    unknown = [signal for signal in signals if signal not in AGENT_PERCEPTION_SIGNALS]
    if unknown:
        return jsonify({
            "ok": False,
            "error": "unknown_signals",
            "unknown": unknown,
            "available": list(AGENT_PERCEPTION_SIGNALS),
        }), 400

    settings = user_store.load_proactive_settings() if hasattr(user_store, "load_proactive_settings") else {}
    state = perception_store.get_state(user_store.user_id)
    snapshot = perception_service.snapshot(user_store.user_id)
    pull_snapshot = perception_service.pull_snapshot(user_store.user_id)

    out: dict[str, Any] = {}
    for signal in signals:
        reason = _permission_states_reason(settings, signal) or _null_state_message_reason(state, signal)
        out[signal] = _disabled(reason) if reason else _signal_doc(signal, snapshot, pull_snapshot)
    return jsonify({"ok": True, "signals": out})


# Agent signal name -> canonical catalog signal key for the quantitative history
# (perception_daily) rollups. Mirrors AGENT_PERCEPTION_SIGNALS where historized.
_HISTORY_SIGNAL_TO_CATALOG: dict[str, str] = {
    "vitals": "health_vitals",
    "steps": "health_vitals",          # step_count lives in the vitals signal
    "sleep": "health_sleep",
    "workout": "health_workout",
    "activity": "health_activity",
    "body": "health_body",
    "metabolic": "health_metabolic",
    "cycle": "health_cycle",
    "mood": "health_mood",
    "weather": "weather",
    "motion": "motion_state",
    "location": "location_signal",
    "calendar": "calendar_next_event",
    "focus": "focus",
    "audio_route": "audio_route",
    "reminders": "reminders",
    "now_playing": "playback",
    "music": "playback",
}


def _history_signal(raw: str | None) -> str | None:
    sig = str(raw or "").strip().lower()
    return _HISTORY_SIGNAL_TO_CATALOG.get(sig)


@bp.route("/v1/agent/perception/trend", methods=["GET"])
def agent_perception_trend():
    """Rolling baseline + delta for one numeric field over the last N days, so
    the agent can sense change vs the user's norm (e.g. RHR up ~14% vs 30d)."""
    user_store = auth.require_user()
    sig = _history_signal(request.args.get("signal"))
    if sig is None:
        return jsonify({"ok": False, "error": "unknown_or_unhistorized_signal",
                        "available": sorted(_HISTORY_SIGNAL_TO_CATALOG)}), 400
    field = (request.args.get("field") or "").strip() or None
    try:
        days = max(1, min(int(request.args.get("days", "30")), 365))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid_days"}), 400
    rows = perception_store.list_perception_daily(user_store.user_id, sig, days)
    return jsonify({"ok": True, "trend": perception_history.read_trend(rows, sig, field)})


@bp.route("/v1/agent/perception/history", methods=["GET"])
def agent_perception_history():
    """Raw per-day rollup docs for a signal over the last N days (the agent sees
    the full daily shape: distributions / totals / event lists / minutes)."""
    user_store = auth.require_user()
    sig = _history_signal(request.args.get("signal"))
    if sig is None:
        return jsonify({"ok": False, "error": "unknown_or_unhistorized_signal",
                        "available": sorted(_HISTORY_SIGNAL_TO_CATALOG)}), 400
    try:
        days = max(1, min(int(request.args.get("days", "14")), 365))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid_days"}), 400
    rows = perception_store.list_perception_daily(user_store.user_id, sig, days)
    return jsonify({"ok": True, "signal": sig, "days": days, "daily": rows})
