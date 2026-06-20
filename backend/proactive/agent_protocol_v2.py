"""Agent protocol helpers for Proactive/Perception Runtime V2.

This module is intentionally pure. It parses the model-facing V2 turn protocol
and builds the context payload without reaching into perception services.
Perception values stay behind tools; wake contexts carry only digests/hints.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_MESSAGE_CHARS_V2 = 4000
MAX_ACTION_NOTE_CHARS_V2 = 1000
MAX_REASON_CHARS_V2 = 500
MAX_ORIGIN_REFS_V2 = 50

ACTION_TYPES_V2 = {
    "send_message",
    "sleep",
    "schedule_wake",
    "cancel_wake",
    "needs_background",
}


@dataclass(frozen=True)
class AgentTurnResponseV2:
    messages: tuple[str, ...] = ()
    actions: tuple[Mapping[str, Any], ...] = ()
    needs_background: bool = False
    background_request: Mapping[str, Any] = field(default_factory=dict)


def _clean_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        return text[:limit]
    return text


def _json_payload_from_text(text: str) -> Mapping[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        stripped = fence.group(1).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _coerce_origin_refs(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    refs: list[str] = []
    for item in value:
        ref = str(item or "").strip()
        if ref and ref not in refs:
            refs.append(ref[:200])
        if len(refs) >= MAX_ORIGIN_REFS_V2:
            break
    return refs


def _coerce_action(raw: Any) -> tuple[dict[str, Any] | None, str | None, Mapping[str, Any] | None]:
    """Return (action, message_text, background_request)."""
    if not isinstance(raw, Mapping):
        return None, None, None
    action_type = str(raw.get("type") or raw.get("action") or "").strip()
    if action_type not in ACTION_TYPES_V2:
        return None, None, None

    if action_type == "send_message":
        text = _clean_text(raw.get("text") or raw.get("message"), MAX_MESSAGE_CHARS_V2)
        if not text:
            return None, None, None
        return {"type": "send_message", "text": text}, text, None

    if action_type == "sleep":
        reason = _clean_text(raw.get("reason") or "not_now", MAX_REASON_CHARS_V2)
        return {"type": "sleep", "reason": reason or "not_now"}, None, None

    if action_type == "schedule_wake":
        at = _clean_text(raw.get("at"), 120)
        tz = _clean_text(raw.get("tz") or raw.get("timezone"), 80)
        if not at or not tz:
            return None, None, None
        action: dict[str, Any] = {
            "type": "schedule_wake",
            "at": at,
            "tz": tz,
            "note": _clean_text(raw.get("note"), MAX_ACTION_NOTE_CHARS_V2),
            "origin_refs": _coerce_origin_refs(raw.get("origin_refs")),
        }
        return action, None, None

    if action_type == "cancel_wake":
        wake_id = _clean_text(raw.get("wake_id") or raw.get("id"), 200)
        if not wake_id:
            return None, None, None
        action = {"type": "cancel_wake", "wake_id": wake_id}
        reason = _clean_text(raw.get("reason"), MAX_REASON_CHARS_V2)
        if reason:
            action["reason"] = reason
        return action, None, None

    request = raw.get("request") if isinstance(raw.get("request"), Mapping) else {}
    action = {"type": "needs_background", "request": dict(request or {})}
    return action, None, dict(request or {})


def parse_agent_response_v2(raw: Any) -> AgentTurnResponseV2:
    """Parse the V2 model turn protocol.

    The parser accepts either a mapping, a JSON string, or an object with
    `messages` / `actions` attributes. Invalid protocol falls back to `sleep`;
    it does not attempt to interpret free-form text as a chat message.
    """
    if isinstance(raw, AgentTurnResponseV2):
        return raw

    if isinstance(raw, Mapping):
        payload: Mapping[str, Any] | None = raw
    elif isinstance(raw, str):
        payload = _json_payload_from_text(raw)
    elif hasattr(raw, "messages") or hasattr(raw, "actions"):
        payload = {
            "messages": getattr(raw, "messages", ()),
            "actions": getattr(raw, "actions", ()),
            "needs_background": getattr(raw, "needs_background", False),
            "background_request": getattr(raw, "background_request", {}),
        }
    else:
        payload = None

    if payload is None:
        return AgentTurnResponseV2(actions=({"type": "sleep", "reason": "invalid_protocol"},))

    messages: list[str] = []
    actions: list[dict[str, Any]] = []

    raw_messages = payload.get("messages") if isinstance(payload.get("messages"), Sequence) else []
    if isinstance(raw_messages, (str, bytes, bytearray)):
        raw_messages = []
    for item in raw_messages:
        text = _clean_text(item, MAX_MESSAGE_CHARS_V2)
        if not text:
            continue
        messages.append(text)
        actions.append({"type": "send_message", "text": text})

    needs_background = bool(payload.get("needs_background"))
    background_request = (
        dict(payload.get("background_request") or {})
        if isinstance(payload.get("background_request"), Mapping)
        else {}
    )

    raw_actions = payload.get("actions") if isinstance(payload.get("actions"), Sequence) else []
    if isinstance(raw_actions, (str, bytes, bytearray)):
        raw_actions = []
    for item in raw_actions:
        action, message_text, request = _coerce_action(item)
        if action is None:
            continue
        if message_text and message_text not in messages:
            messages.append(message_text)
        if action not in actions:
            actions.append(action)
        if request is not None:
            needs_background = True
            if request:
                background_request = dict(request)

    if needs_background and not any(action.get("type") == "needs_background" for action in actions):
        actions.append({"type": "needs_background", "request": dict(background_request or {})})

    if not actions and not needs_background:
        actions.append({"type": "sleep", "reason": "not_now"})

    return AgentTurnResponseV2(
        messages=tuple(messages),
        actions=tuple(actions),
        needs_background=needs_background,
        background_request=background_request,
    )


def turn_outcome_from_agent_response_v2(outcome_cls: Any, response: AgentTurnResponseV2) -> Any:
    return outcome_cls(
        messages=response.messages,
        actions=response.actions,
        needs_background=response.needs_background,
        background_request=response.background_request,
    )


def _iso_time(ts: Any) -> str:
    try:
        value = float(ts)
    except (TypeError, ValueError):
        value = 0.0
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _local_iso_time(ts: Any, tz_name: Any) -> tuple[str, str]:
    tz = str(tz_name or "").strip() or "UTC"
    try:
        zone = ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        tz = "UTC"
        zone = timezone.utc
    try:
        value = float(ts)
    except (TypeError, ValueError):
        value = 0.0
    return datetime.fromtimestamp(value, tz=zone).isoformat(), tz


def build_agent_context_v2(
    merged_context: Any,
    *,
    recent_chat: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the model-facing V2 turn context.

    `user_message` turns intentionally omit perception digests/hints. The agent
    can pull perception via tools when needed, but raw snapshot values are not
    passively injected into the prompt.
    """
    trigger = str(getattr(merged_context, "trigger", "") or "")
    user_led = trigger == "user_message"
    local_time, timezone_name = _local_iso_time(
        getattr(merged_context, "created_at", 0.0),
        getattr(merged_context, "timezone", ""),
    )
    context: dict[str, Any] = {
        "trigger": trigger,
        "merged_triggers": list(getattr(merged_context, "merged_triggers", ()) or ()),
        "latency_sensitive": bool(getattr(merged_context, "latency_sensitive", False)),
        "manual": bool(getattr(merged_context, "manual", False)),
        "time": _iso_time(getattr(merged_context, "created_at", 0.0)),
        "local_time": local_time,
        "timezone": timezone_name,
        "created_at": float(getattr(merged_context, "created_at", 0.0) or 0.0),
        "switches": dict(getattr(merged_context, "switches", {}) or {}),
        "wake_ids": list(getattr(merged_context, "wake_ids", ()) or ()),
        "tools": [dict(item) for item in (getattr(merged_context, "tools", ()) or ())],
        "recent_chat": [dict(item) for item in (recent_chat or ())],
    }
    background_payloads = [dict(item) for item in (getattr(merged_context, "background_payloads", ()) or ())]
    if background_payloads:
        context["background_payloads"] = background_payloads

    if user_led:
        return context

    change_digest = str(getattr(merged_context, "change_digest", "") or "")
    if change_digest:
        context["change_digest"] = change_digest
    presence_hints = dict(getattr(merged_context, "presence_hints", {}) or {})
    if presence_hints:
        context["presence_hints"] = presence_hints
    scheduled_note = str(getattr(merged_context, "scheduled_note", "") or "")
    if scheduled_note:
        context["scheduled_note"] = scheduled_note
    origin_refs = list(getattr(merged_context, "origin_refs", ()) or ())
    if origin_refs:
        context["origin_refs"] = origin_refs

    return context


def visible_message_count_v2(outcome: Any) -> int:
    return len(tuple(getattr(outcome, "messages", ()) or ()))


def manual_contract_violation_v2(merged_context: Any, outcome: Any) -> str:
    if not bool(getattr(merged_context, "manual", False)):
        return ""
    if visible_message_count_v2(outcome) > 0:
        return ""
    return "ignored_manual"


def actions_for_persistence_v2(outcome: Any) -> tuple[Mapping[str, Any], ...]:
    return tuple(dict(item) for item in (getattr(outcome, "actions", ()) or ()) if isinstance(item, Mapping))
