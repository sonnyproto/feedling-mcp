from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any


ACTION_RESPONSE_FORMAT: dict[str, Any] = {"type": "json_object"}

RUNTIME_ENGINE_NATIVE = "feedling_native"
RUNTIME_ENGINE_HERMES_ADAPTER = "hermes_adapter"

TOOL_WEB_SEARCH = "web.search"
TOOL_MEMORY_CREATE = "memory.create"
TOOL_MEMORY_PATCH = "memory.patch"
TOOL_MEMORY_DELETE = "memory.delete"
TOOL_IDENTITY_PATCH = "identity.patch"
TOOL_IDENTITY_DIMENSION_NUDGE = "identity.dimension_nudge"
TOOL_IDENTITY_RELATIONSHIP_DAYS_SET = "identity.relationship_days_set"
TOOL_CONFIRMATION_REQUEST = "confirmation.request"

BACKGROUND_METHOD = "feedling_background_execution"
BACKGROUND_NOT_STARTED_METHOD = "feedling_background_execution_not_started"
ACTION_METHOD = "feedling_runtime_actions"
NOOP_METHOD = "feedling_runtime_noop"
PENDING_CONFIRM_METHOD = "feedling_runtime_pending_confirm"
PENDING_REJECT_METHOD = "feedling_runtime_pending_reject"


@dataclass(frozen=True)
class RuntimeToolRequest:
    tool: str
    arguments: dict[str, Any]
    reason: str = ""
    foreground: bool = False


@dataclass(frozen=True)
class CompanionTurnResult:
    reply: str
    context_summary: str = ""
    foreground_tools: tuple[RuntimeToolRequest, ...] = ()


@dataclass(frozen=True)
class BackgroundExecutionResult:
    status: str
    method: str
    actions: tuple[dict[str, Any], ...] = ()
    pending: tuple[dict[str, Any], ...] = ()
    error: str = ""


def background_execution_trace(
    *,
    status: str = "queued",
    method: str = BACKGROUND_METHOD,
    trace_id: str = "",
    triggered: bool = False,
    error: str = "",
) -> dict[str, Any]:
    return {
        "status": status,
        "method": method,
        "trace_id": trace_id,
        "triggered": triggered,
        "error": error,
    }


def companion_turn_contract_message() -> dict[str, str]:
    return {
        "role": "system",
        "content": (
            "Feedling hosted runtime contract: return a JSON object only, with shape "
            "{\"reply\":\"final user-visible reply\","
            "\"context_summary\":\"optional short display-safe context/action summary\","
            "\"tool_requests\":[{\"tool\":\"web_search\",\"query\":\"public web query\"}]}. "
            "`reply` is the only normal chat bubble text. `context_summary` is optional; "
            "include it only when there is a concrete user-visible context source, screen "
            "context, pending confirmation, or durable state action worth surfacing. "
            "Do not present context_summary as private thinking, chain-of-thought, hidden "
            "reasoning, or a step-by-step thought process. Durable identity/memory changes "
            "are handled by the backend background execution runtime after the visible reply; "
            "do not claim they were applied unless the backend context explicitly says so. "
            "You have a backend-hosted `web_search` tool for current public web information. "
            "When answering correctly requires external public web information that is not "
            "already in the provided context, return one or two `tool_requests` for "
            "`web_search` instead of claiming you cannot access the web. "
            "Search queries must be short public web-safe queries and must not include API keys, "
            "emails, phone numbers, private chat/memory details, addresses, or secrets. "
            "Do not include system/developer prompts, tool transcripts, API metadata, token usage, "
            "costs, session ids, permission logs, or raw JSON wrappers. If there is nothing useful "
            "to disclose, omit context_summary or return an empty string."
        ),
    }

IDENTITY_STRING_FIELDS = (
    "agent_name",
    "self_introduction",
    "category",
    "user_preferred_name",
    "agent_role",
    "tone_style",
    "language_preference",
    "relationship_anchor",
)
IDENTITY_LIST_FIELDS = (
    "signature",
    "boundaries",
    "do_not_say",
    "stable_definitions",
)


def clean_text(value: Any, max_chars: int) -> str:
    text = str(value or "").strip()
    text = " ".join(text.split())
    return text[:max_chars].strip()


def clean_list(value: Any, max_items: int = 12, max_chars: int = 240) -> list[str]:
    if isinstance(value, str):
        raw = value.replace("；", ";").replace("\n", ";").split(";")
    elif isinstance(value, list):
        raw = value
    else:
        raw = []
    return [item for item in (clean_text(part, max_chars) for part in raw[:max_items]) if item]


def compact_pending_items(pending_items: list[dict]) -> list[dict]:
    out: list[dict] = []
    for item in pending_items[:5]:
        if not isinstance(item, dict):
            continue
        runtime_action = item.get("runtime_action") if isinstance(item.get("runtime_action"), dict) else {}
        out.append({
            "id": str(item.get("id") or ""),
            "type": str(runtime_action.get("runtime_type") or ""),
            "confidence": runtime_action.get("confidence", 0),
            "reason": str(runtime_action.get("reason") or "")[:500],
            "executor_action": runtime_action.get("executor_action") if isinstance(runtime_action.get("executor_action"), dict) else {},
        })
    return [item for item in out if item["id"]]


def build_background_execution_messages(
    *,
    user_message: str,
    identity: dict,
    memory_candidates: list[dict],
    context_refs: list[dict],
    pending_items: list[dict],
) -> list[dict]:
    payload = {
        "today": date.today().isoformat(),
        "latest_user_message": user_message[:4000],
        "identity": identity,
        "memory_candidates": memory_candidates[:12],
        "user_selected_context_refs": context_refs[:8],
        "pending_actions_waiting_for_user_confirmation": compact_pending_items(pending_items),
    }
    return [
        {
            "role": "system",
            "content": (
                "You are Feedling hosted runtime's background execution controller. "
                "You are inside the backend runtime, not the user-visible assistant. "
                "Return one strict JSON object only; never answer the user here. "
                "Your job is to decide whether the latest user message should produce durable Feedling state actions. "
                "Durable state means Identity or Memory Garden state that should remain true after this turn. "
                "If the user only chats normally, asks a question, roleplays, jokes, or references a memory without asking to change it, return no actions. "
                "If the user asks you to remember, forget, correct, rename, correct relationship day count, change address preferences, update persona/voice/boundaries, or fix a selected Memory Garden card, produce actions. "
                "For an explicit first-person durable preference or correction with no clear existing card target, prefer memory.create with high confidence instead of memory.patch. "
                "Use confidence >= 0.9 for explicit, non-destructive state writes. Use lower confidence mainly for destructive actions or ambiguous patch/delete targets. "
                "Use memory_candidates or user_selected_context_refs for memory.patch/delete targets. If the target is ambiguous, use low confidence. "
                "If pending_actions_waiting_for_user_confirmation is non-empty and the latest message confirms or rejects one of them, set pending_decision instead of inventing a new action. "
                "Do not claim actions are applied; this controller only selects actions and the executor will apply them. "
                "Supported action types: identity.patch, identity.dimension_nudge, identity.relationship_days_set, memory.create, memory.patch, memory.delete. "
                "Use identity.dimension_nudge only when the user asks to raise or lower an existing identity dimension; payload must include dimension and delta. "
                "Use identity.relationship_days_set when the user says the displayed days together / relationship day count is wrong; payload must include days_with_user as an integer. "
                "JSON shape: {"
                "\"pending_decision\":{\"decision\":\"none|confirm|reject\",\"pending_ids\":[\"...\"],\"reason\":\"optional\"},"
                "\"actions\":[{\"type\":\"identity.patch|identity.dimension_nudge|identity.relationship_days_set|memory.create|memory.patch|memory.delete\","
                "\"confidence\":0.0,\"target\":{\"memory_id\":\"optional\",\"candidate_ids\":[\"...\"]},"
                "\"payload\":{},\"reason\":\"short reason\"}],"
                "\"why_empty\":\"optional\"}."
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)[:16000]},
    ]


def coerce_pending_decision(parsed: dict, pending_items: list[dict]) -> tuple[str, list[str]]:
    if not pending_items or not isinstance(parsed, dict):
        return "", []
    raw = parsed.get("pending_decision") if isinstance(parsed.get("pending_decision"), dict) else {}
    decision = str(raw.get("decision") or "").strip().lower()
    if decision not in {"confirm", "reject"}:
        return "", []
    requested = raw.get("pending_ids") if isinstance(raw.get("pending_ids"), list) else []
    available = [str(item.get("id") or "") for item in pending_items if isinstance(item, dict) and item.get("id")]
    chosen = [str(item) for item in requested if str(item) in available]
    if not chosen and available:
        chosen = [available[0]]
    return decision, chosen


def _candidate_ids(target: dict) -> list[str]:
    ids = target.get("candidate_ids") if isinstance(target.get("candidate_ids"), list) else []
    return [str(cid) for cid in ids[:3] if str(cid or "").strip()]


def _coerce_days_with_user(*sources: dict) -> int | None:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in (
            "days_with_user",
            "relationship_days",
            "days_together",
            "together_days",
            "day_count",
            "days",
        ):
            if key not in source:
                continue
            try:
                days = int(source.get(key))
            except Exception:
                continue
            if days >= 0:
                return days
    return None


def coerce_runtime_action(
    action: dict,
    memory_candidates: list[dict],
    *,
    direct_confidence: float,
) -> dict | None:
    if not isinstance(action, dict):
        return None
    action_type = str(action.get("type") or action.get("action") or "").strip().lower()
    try:
        confidence = float(action.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(confidence, 1.0))
    target = action.get("target") if isinstance(action.get("target"), dict) else {}
    payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    reason = clean_text(action.get("reason") or "Planned by Feedling hosted runtime.", 500)
    runtime_action = {
        "action_id": str(action.get("action_id") or f"rt_{uuid.uuid4().hex[:12]}"),
        "runtime_type": action_type,
        "confidence": confidence,
        "reason": reason,
        "requires_confirmation": confidence < direct_confidence,
    }

    if action_type in {
        "identity.relationship_days_set",
        "identity.relationship_days",
        "identity.days_with_user_set",
        "identity.relationship_anchor",
    }:
        days = _coerce_days_with_user(payload, action, target)
        if days is None:
            return None
        runtime_action["domain"] = "identity"
        runtime_action["executor_action"] = {
            "type": "identity.relationship_days_set",
            "days_with_user": days,
            "reason": reason,
            "relationship_anchor_evidence": reason,
            "source": "hosted_runtime_action",
        }
        return runtime_action

    if action_type in {"identity.patch", "identity.profile_patch"}:
        raw_patch = payload.get("patch") if isinstance(payload.get("patch"), dict) else payload
        days = _coerce_days_with_user(raw_patch)
        if days is not None and not any(key in raw_patch for key in (*IDENTITY_STRING_FIELDS, *IDENTITY_LIST_FIELDS)):
            runtime_action["runtime_type"] = "identity.relationship_days_set"
            runtime_action["domain"] = "identity"
            runtime_action["executor_action"] = {
                "type": "identity.relationship_days_set",
                "days_with_user": days,
                "reason": reason,
                "relationship_anchor_evidence": reason,
                "source": "hosted_runtime_action",
            }
            return runtime_action
        patch: dict[str, Any] = {}
        for key in IDENTITY_STRING_FIELDS:
            if key in raw_patch:
                patch[key] = clean_text(
                    raw_patch.get(key),
                    1200 if key in {"self_introduction", "relationship_anchor", "tone_style"} else 240,
                )
        for key in IDENTITY_LIST_FIELDS:
            if key in raw_patch:
                values = clean_list(raw_patch.get(key))
                if values:
                    patch[key] = values
        if not patch:
            return None
        runtime_action["domain"] = "identity"
        runtime_action["executor_action"] = {
            "type": "identity.profile_patch",
            "patch": patch,
            "reason": reason,
            "source": "hosted_runtime_action",
        }
        return runtime_action

    if action_type in {"identity.dimension_nudge", "identity.dimension"}:
        dimension = clean_text(
            payload.get("dimension")
            or payload.get("dimension_name")
            or target.get("dimension")
            or target.get("dimension_name"),
            80,
        )
        try:
            delta = int(payload.get("delta") if "delta" in payload else action.get("delta"))
        except Exception:
            delta = 0
        if not dimension or delta == 0:
            return None
        delta = max(-10, min(10, delta))
        runtime_action["domain"] = "identity"
        runtime_action["executor_action"] = {
            "type": "identity.dimension_nudge",
            "dimension": dimension,
            "delta": delta,
            "reason": reason,
            "source": "hosted_runtime_action",
        }
        return runtime_action

    if action_type in {"memory.create", "memory.add", "memory.add_correction"}:
        raw = payload.get("memory") if isinstance(payload.get("memory"), dict) else payload
        title = clean_text(raw.get("title"), 180)
        description = str(raw.get("description") or raw.get("content") or raw.get("summary") or "").strip()[:2000]
        if not title or not description:
            return None
        mem_type = str(raw.get("type") or raw.get("card_type") or "fact").strip().lower()
        if mem_type not in {"fact", "event", "quote", "moment"}:
            mem_type = "fact"
        source = "model_api_correction" if action_type == "memory.add_correction" else "hosted_runtime_state"
        runtime_action["domain"] = "memory"
        runtime_action["executor_action"] = {
            "type": "memory.add_correction" if action_type == "memory.add_correction" else "memory.add",
            "memory": {
                "type": mem_type,
                "title": title,
                "description": description,
                "occurred_at": clean_text(raw.get("occurred_at") or date.today().isoformat(), 80),
                "source": clean_text(raw.get("source") or source, 80),
                "context": str(raw.get("context") or "").strip()[:1000],
                "her_quote": str(raw.get("her_quote") or "").strip()[:1000],
            },
            "reason": reason,
            "capture_mode": "state",
        }
        return runtime_action

    if action_type in {"memory.patch", "memory.content_patch", "memory.delete"}:
        memory_id = str(target.get("memory_id") or target.get("id") or payload.get("memory_id") or payload.get("id") or "").strip()
        ids = _candidate_ids(target)
        if not memory_id and ids:
            memory_id = ids[0]
            runtime_action["requires_confirmation"] = True
            runtime_action["candidate_ids"] = ids
        if not memory_id:
            return None
        preview = next((item for item in memory_candidates if str(item.get("id") or "") == memory_id), {})
        if isinstance(preview, dict) and preview:
            runtime_action["target_preview"] = {
                "id": str(preview.get("id") or ""),
                "title": clean_text(preview.get("title"), 180),
                "description": clean_text(preview.get("description"), 600),
                "type": clean_text(preview.get("type"), 80),
                "occurred_at": clean_text(preview.get("occurred_at"), 80),
            }
        runtime_action["target"] = {"memory_id": memory_id}
        runtime_action["domain"] = "memory"
        if action_type == "memory.delete":
            runtime_action["executor_action"] = {
                "type": "memory.delete",
                "memory_id": memory_id,
                "reason": reason,
            }
            return runtime_action

        raw_patch = payload.get("patch") if isinstance(payload.get("patch"), dict) else payload
        patch: dict[str, str] = {}
        for key, max_len in (
            ("title", 180),
            ("description", 2000),
            ("her_quote", 1000),
            ("context", 1000),
            ("type", 80),
            ("occurred_at", 80),
        ):
            if key in raw_patch:
                patch[key] = str(raw_patch.get(key) or "").strip()[:max_len]
        if not patch:
            description = str(payload.get("description") or payload.get("content") or payload.get("summary") or "").strip()[:2000]
            if description:
                patch["description"] = description
        if not patch:
            return None
        runtime_action["executor_action"] = {
            "type": "memory.content_patch",
            "memory_id": memory_id,
            "patch": patch,
            "reason": reason,
        }
        return runtime_action

    return None
