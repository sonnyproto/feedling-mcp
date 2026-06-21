"""Multi-turn tool-execution loop for the V2 proactive agent (D11).

Provider-agnostic and transport-agnostic: `call_model` maps messages->reply text;
`call_tool` runs one tool and returns its result dict. Hosted injects an
in-process ToolExecutorV2; resident injects an HTTP call to
/v1/proactive/tool/execute. The loop runs the model's tool_calls, feeds results
back, and re-calls until a terminal turn (no tool_calls) or a hard cap.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from proactive.agent_protocol_v2 import parse_agent_response_v2, agent_tool_calls_v2

CallModel = Callable[[list[dict[str, Any]]], str]
CallTool = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]


def _render_tool_results(results: list[Mapping[str, Any]]) -> str:
    return ("Tool results (JSON). Use them to continue; call more tools or finish "
            "with messages/actions:\n" + json.dumps(results, ensure_ascii=False, default=str))


def run_tool_loop_v2(call_model: CallModel, call_tool: CallTool,
                     base_messages: list[dict[str, Any]], *, max_iters: int = 4) -> str:
    messages = list(base_messages)
    reply = ""
    for _ in range(max_iters):
        reply = call_model(messages)
        calls = agent_tool_calls_v2(parse_agent_response_v2(reply))
        if not calls:
            return reply  # terminal turn

        results: list[dict[str, Any]] = []
        handoff: dict[str, Any] | None = None
        for name, args in calls:
            res = dict(call_tool(name, args) or {})
            res.setdefault("name", name)
            results.append(res)
            if res.get("needs_background"):
                # Budget handoff: stop running the rest of this turn's calls —
                # we've decided to defer, so further inline executions (extra
                # work, and an HTTP round-trip each on the resident path) would
                # defeat the handoff. Attribute to this first trigger.
                handoff = {"tool": name, "args": dict(args)}
                break

        if handoff is not None:
            return json.dumps({"actions": [{"type": "needs_background", "request": handoff}]})

        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": _render_tool_results(results)})

    return reply  # cap reached
