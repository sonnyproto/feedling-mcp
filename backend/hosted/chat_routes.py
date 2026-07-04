"""Hosted chat: /v1/model_api/chat/send."""

import json

from core import util as core_util
from core.store import UserStore

from model_api_runtime import memory_tools as hosted_memory_tools
from proactive.agent_protocol_v2 import parse_agent_response_v2, agent_tool_calls_v2
from proactive.tool_catalog_v2 import foreground_chat_tool_catalog_v2, foreground_chat_tool_context_v2
from proactive.tool_executor_v2 import (
    ToolBudgetV2,
    ToolCallV2,
    ToolExecutorV2,
    combined_runtime_adapters_v2,
)
from accounts import runtime_auth
import provider_client
from hosted import chat_send_core
from hosted import config_store as hosted_config_store
from hosted import turn as hosted_turn




HOSTED_CHAT_FULL_TOOL_LOOP_V2_FLAG = "hosted_chat_full_tool_loop_v2_enabled"
FOREGROUND_CHAT_TOOL_BUDGET_MODE_V2 = "foreground_chat_fast"


def _hosted_chat_full_tool_loop_v2_enabled(store: UserStore) -> bool:
    try:
        config = hosted_config_store._load_model_api_config(store)
        profile = hosted_config_store._ensure_model_api_runtime_profile(store, config) or {}
        if HOSTED_CHAT_FULL_TOOL_LOOP_V2_FLAG in profile:
            return bool(profile.get(HOSTED_CHAT_FULL_TOOL_LOOP_V2_FLAG))
        if HOSTED_CHAT_FULL_TOOL_LOOP_V2_FLAG in (config or {}):
            return bool((config or {}).get(HOSTED_CHAT_FULL_TOOL_LOOP_V2_FLAG))
        return core_util.runtime_v2_default_on()
    except Exception:
        return False


def _agent_tool_calls_from_reply(raw_reply: str) -> list[tuple[str, dict]]:
    try:
        return agent_tool_calls_v2(parse_agent_response_v2(raw_reply))
    except Exception:
        return []


def _model_api_chat_tool_calls(
    raw_reply: str,
    *,
    memory_tools_enabled: bool,
    perception_tools_enabled: bool,
) -> list[tuple[str, dict, str]]:
    allowed_perception = {tool["name"] for tool in foreground_chat_tool_context_v2()}
    calls: list[tuple[str, dict, str]] = []
    for name, args in _agent_tool_calls_from_reply(raw_reply):
        if memory_tools_enabled and name in {
            hosted_memory_tools.MEMORY_INDEX_TOOL,
            hosted_memory_tools.MEMORY_FETCH_TOOL,
        }:
            calls.append((name, args, "memory"))
        elif perception_tools_enabled:
            calls.append((name, args, "perception" if name in allowed_perception else "foreground_unavailable"))
    return calls


def _foreground_tool_unavailable_result(name: str, *, reason: str) -> dict:
    return {
        "ok": False,
        "name": name,
        "outcome": "unavailable",
        "result": {},
        "error": reason,
        "error_code": reason,
        "error_message": "This tool is not available in foreground chat.",
        "needs_background": False,
    }


def _run_model_api_memory_tool_loop(
    runtime,
    provider_messages: list[dict],
    *,
    store,
    api_key: str | None,
    max_tokens: int,
    temperature: float,
    memory_tools_enabled: bool = True,
    perception_tools_enabled: bool = False,
) -> tuple[dict, str, dict]:
    messages = list(provider_messages)
    memory_trace: dict = {
        "mode": "agent_tools",
        "index_called": False,
        "fetch_called": False,
        "tool_calls": [],
        "fetched_ids": [],
        "cumulative_fetch_limit": hosted_memory_tools.MEMORY_FETCH_CUMULATIVE_LIMIT,
    } if memory_tools_enabled else {}
    perception_trace: dict = {
        "mode": "additive_foreground_perception",
        "budget_mode": FOREGROUND_CHAT_TOOL_BUDGET_MODE_V2,
        "tool_calls": [],
        "available_tools": sorted(tool["name"] for tool in foreground_chat_tool_context_v2()),
    } if perception_tools_enabled else {}
    executor = ToolExecutorV2(
        catalog=foreground_chat_tool_catalog_v2(),
        adapters=combined_runtime_adapters_v2(api_key, store),
        budget=_foreground_chat_tool_budget_v2(),
    ) if perception_tools_enabled else None
    result: dict = {}
    raw_reply = ""
    usage_rounds: list[dict] = []
    for _ in range(4 if perception_tools_enabled else 3):
        result = provider_client.chat_completion(
            runtime,
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=90.0,
            include_reasoning=hosted_turn.MODEL_API_PROVIDER_REASONING_ENABLED,
        )
        raw_reply = str(result.get("reply") or "").strip()
        usage_rounds.append(result.get("usage") or {})
        calls = _model_api_chat_tool_calls(
            raw_reply,
            memory_tools_enabled=memory_tools_enabled,
            perception_tools_enabled=perception_tools_enabled,
        )
        if not calls:
            break
        tool_results: list[dict] = []
        for name, args, kind in calls:
            if kind == "memory":
                try:
                    tool_results.append(
                        hosted_memory_tools.execute_memory_tool(
                            store,
                            api_key,
                            name,
                            args,
                            trace=memory_trace,
                        )
                    )
                except Exception as e:
                    memory_trace.setdefault("tool_calls", []).append({
                        "name": name,
                        "ok": False,
                        "error": f"{type(e).__name__}:{str(e)[:160]}",
                    })
                    tool_results.append({"ok": False, "name": name, "error": "memory_tool_failed"})
                continue
            if kind == "foreground_unavailable" or executor is None:
                result_doc = _foreground_tool_unavailable_result(name, reason="foreground_tool_unavailable")
                tool_results.append(result_doc)
                if perception_trace:
                    perception_trace["tool_calls"].append({
                        "name": name,
                        "ok": False,
                        "outcome": "unavailable",
                        "error_code": "foreground_tool_unavailable",
                    })
                continue
            res = executor.execute(ToolCallV2(name=name, args=dict(args or {}), user_id=store.user_id)).as_dict()
            if res.get("needs_background"):
                res = _foreground_tool_unavailable_result(name, reason="foreground_slow_tool_unavailable")
            tool_results.append(res)
            if perception_trace:
                perception_trace["tool_calls"].append({
                    "name": name,
                    "ok": bool(res.get("ok")),
                    "outcome": str(res.get("outcome") or ""),
                    "error_code": str(res.get("error_code") or ""),
                    "needs_background": bool(res.get("needs_background")),
                    "cost_class": ((res.get("trace") or {}) if isinstance(res.get("trace"), dict) else {}).get("cost_class", ""),
                })
        messages.append({"role": "assistant", "content": raw_reply[:4000]})
        messages.append({"role": "user", "content": hosted_memory_tools.render_memory_tool_results(tool_results)})
    if len(usage_rounds) > 1:
        result = {**result, "usage": {"memory_tool_loop": usage_rounds, "final": result.get("usage") or {}}}
    if perception_trace:
        memory_trace["foreground_perception_v2"] = perception_trace
    return result, raw_reply, memory_trace


def _model_api_foreground_perception_tool_instruction_message() -> dict:
    tools_json = json.dumps(
        foreground_chat_tool_context_v2(),
        ensure_ascii=False,
        sort_keys=True,
    )
    return {
        "role": "system",
        "content": (
            "Additional fast foreground perception tools are available for the current chat turn. "
            "They are additive: keep using the normal foreground chat contract and any separate memory-tool instructions exactly as given. "
            "To gather current perception data, return "
            "{\"tool_calls\":[{\"name\":\"<tool.name>\",\"args\":{...}}]}; the runtime will return tool results. "
            "When finished, return the normal hosted chat final JSON required by the earlier turn contract, e.g. {\"reply\":\"...\"}. "
            "Only the listed fast tools are available here. Do not call memory.*, action tools, steps, sleep, workout, vitals, photo, screen, or long calendar windows. "
            "If a needed tool is not listed, answer from available context and do not promise a background follow-up. "
            "Do not include change_digest or proactive wake assumptions in foreground chat. "
            "Available tools JSON:\n" + tools_json
        ),
    }


def _foreground_chat_tool_budget_v2() -> ToolBudgetV2:
    return ToolBudgetV2(slow_inline_limit=0)


def _memory_fallback_instruction_message(
    fallback_source: str,
    fallback_memories: list,
    context_memory_trace: dict,
) -> dict:
    fallback_json = json.dumps({
        "source": fallback_source,
        "context_memories": fallback_memories[:8],
        "context_memory_trace": context_memory_trace or {},
    }, ensure_ascii=False)[:8000]
    return {
        "role": "system",
        "content": (
            "Memory fallback was triggered because the first answer did not call memory tools. "
            "The memory fallback JSON below is relevant fallback context for the latest user message. "
            "Priority ladder for conflict resolution: Safety/privacy boundaries >= the user's current explicit "
            "message or correction > directly relevant fallback memory > conflicting assistant draft from before "
            "this fallback. "
            "Do not use fallback memory to argue against the user's current correction. If the user now corrects "
            "or updates a fact, follow the current user message and treat older memory as possibly stale. "
            "If fallback memory directly answers the latest user message, use it instead of any conflicting "
            "assistant draft from before this fallback; do not say you are unsure or ask the user to tell you again. "
            "Judge fallback memories by whether their content directly answers the latest user message, not by "
            "weak/generic/approximate trace labels alone. If fallback memory is only tangentially related, do not "
            "make a hard factual claim from it; say you are not sure rather than over-asserting. "
            "Do not mention memory fallback, tools, traces, or JSON to the user.\n"
            "Memory fallback JSON:\n" + fallback_json
        ),
    }

