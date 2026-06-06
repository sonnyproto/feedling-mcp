from __future__ import annotations

import json
from datetime import date
from typing import Any


def build_foreground_chat_messages(
    *,
    context_payload: dict[str, Any],
    recent_messages: list[dict[str, Any]],
    user_message: str,
) -> list[dict[str, Any]]:
    """Build the user-visible chat turn messages.

    Feedling owns the runtime container, tool contract, safety boundaries, and
    durable-state rules. The imported agent profile, identity, memory, and chat
    history own the companion persona.
    """
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "You are running inside Feedling's hosted runtime for the user's existing AI companion. "
                "Continue the imported companion identity, voice, relationship, boundaries, and preferences "
                "from Agent Profile, Feedling Identity, relevant memory cards, and recent chat. "
                "Do not invent a new persona, role, name, or relationship. "
                "Reply naturally and concisely in the companion's established voice. "
                "Use provided identity and memory context only when relevant. "
                "Memory cards with source=model_api_correction are explicit user corrections; treat them as higher priority than older conflicting memories or identity text. "
                "If a correction says not to repeat a phrase, persona, joke, name, boundary, or setting, do not repeat it. "
                "If identity.agent_name is empty, do not invent or use a name for yourself; wait for the user to name you. "
                "If the user asks to remember, forget, rename, or change Identity/Memory, respond naturally but do not claim the durable state has already been written or deleted. "
                "If Feedling context includes pending_state_updates and the user confirms or cancels them, acknowledge the user's choice naturally; the backend runtime will apply or clear the durable state after this visible reply. "
                "Do not mention hidden implementation details, API keys, prompts, or encrypted storage."
            ),
        },
        {
            "role": "system",
            "content": "Feedling runtime context JSON:\n" + json.dumps(context_payload, ensure_ascii=False)[:12000],
        },
    ]
    for msg in recent_messages[-14:]:
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        role = "assistant" if msg.get("role") in {"openclaw", "agent", "assistant"} else "user"
        messages.append({"role": role, "content": content[:4000]})
    if not any(m.get("role") == "user" and m.get("content") == user_message for m in messages[-3:]):
        messages.append({"role": "user", "content": user_message})
    return messages


def build_pending_confirmation_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "role": "system",
            "content": (
                "You are the user-visible AI companion, speaking in your established voice. "
                "Feedling has NOT applied the pending Identity/Memory update yet. "
                "Ask the user for confirmation naturally, but explicitly state the exact target and intended change. "
                "Do not use a generic stock template like 'I found a possible identity or memory change'. "
                "Do not claim the update is already written. "
                "Tell the user they can reply confirm/cancel or correct the change. "
                "Return JSON only: {\"reply\":\"...\",\"context_summary\":\"...\"}. "
                "`context_summary` is optional and should name the confirmation target/action, "
                "not private reasoning."
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)[:8000]},
    ]


def build_memory_capture_messages(
    *,
    user_message: str,
    assistant_reply: str,
    context_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    payload = {
        "user_message": user_message[:4000],
        "assistant_reply": assistant_reply[:4000],
        "existing_context_memories": (context_payload.get("context_memories") or [])[:8],
        "identity": context_payload.get("identity") or {},
        "agent_profile": context_payload.get("agent_profile") or {},
        "today": date.today().isoformat(),
    }
    return [
        {
            "role": "system",
            "content": (
                "You are Feedling's Memory Capture worker. Return JSON only. "
                "Extract durable Memory Garden cards from the latest exchange. "
                "Only write facts, events, quotes, or rare relational moments. "
                "Do write explicit user corrections about names, boundaries, persona, voice, preferences, or facts if they are not already present in existing_context_memories. "
                "Do not write vague preferences, duplicate existing memories, repeated correction cards, or private implementation details. "
                "Shape: {\"memories\":[{\"type\":\"fact|event|quote|moment\","
                "\"title\":\"...\",\"description\":\"...\",\"occurred_at\":\"YYYY-MM-DD\","
                "\"her_quote\":\"optional\",\"context\":\"optional\"}]}. "
                "Return {\"memories\":[]} if nothing durable should be written."
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)[:12000]},
    ]


def build_web_search_results_message(web_search: dict[str, Any]) -> dict[str, str]:
    payload = {
        "tool": "web_search",
        "status": web_search.get("status", ""),
        "result_count": web_search.get("result_count", 0),
        "results": web_search.get("results", []),
        "errors": web_search.get("errors", []),
    }
    return {
        "role": "system",
        "content": (
            "Backend web_search tool results JSON:\n"
            + json.dumps(payload, ensure_ascii=False)[:12000]
        ),
    }


def web_search_followup_message() -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            "Use the backend web_search results above to answer the original user message. "
            "Return the normal Feedling chat turn JSON only."
        ),
    }
