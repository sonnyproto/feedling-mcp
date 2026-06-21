from __future__ import annotations

import json
import sys
import types
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import chat_routes  # noqa: E402
from proactive.agent_protocol_v2 import parse_agent_response_v2  # noqa: E402
from proactive.tool_executor_v2 import ToolRuntimeAdaptersV2  # noqa: E402


def test_prompt_level_memory_tool_loop_feeds_results_back(monkeypatch):
    scripted = [
        {"reply": json.dumps({"tool_calls": [{"name": "memory_index", "args": {"query": "猫叫什么"}}]})},
        {"reply": json.dumps({"tool_calls": [{"name": "memory_fetch", "args": {"ids": ["mem_cat"]}}]})},
        {"reply": json.dumps({"reply": "记得，你家猫叫武松。"})},
    ]
    model_inputs: list[list[dict]] = []
    tool_calls: list[tuple[str, dict]] = []

    def fake_chat_completion(runtime, messages, **kwargs):
        model_inputs.append([dict(message) for message in messages])
        return scripted.pop(0)

    def fake_execute_memory_tool(store, api_key, name, args, *, trace):
        tool_calls.append((name, dict(args)))
        if name == "memory_index":
            trace["index_called"] = True
            trace["tool_calls"].append({"name": name, "ok": True, "item_count": 1})
            return {"ok": True, "name": name, "items": [{"id": "mem_cat", "summary": "用户家猫叫武松。"}]}
        if name == "memory_fetch":
            trace["fetch_called"] = True
            trace["fetched_ids"] = ["mem_cat"]
            trace["tool_calls"].append({"name": name, "ok": True, "ids": ["mem_cat"], "item_count": 1})
            return {"ok": True, "name": name, "items": [{"id": "mem_cat", "verbatim": "我家猫叫武松。"}]}
        raise AssertionError(name)

    monkeypatch.setattr(chat_routes.provider_client, "chat_completion", fake_chat_completion)
    monkeypatch.setattr(chat_routes.hosted_memory_tools, "execute_memory_tool", fake_execute_memory_tool)

    result, raw_reply, trace = chat_routes._run_model_api_memory_tool_loop(
        object(),
        [{"role": "system", "content": "s"}, {"role": "user", "content": "你还记得我家猫叫什么吗？"}],
        store=types.SimpleNamespace(user_id="usr_loop"),
        api_key="key_loop",
        max_tokens=256,
        temperature=0.1,
    )

    assert json.loads(raw_reply)["reply"] == "记得，你家猫叫武松。"
    assert result["reply"] == raw_reply
    assert tool_calls == [
        ("memory_index", {"query": "猫叫什么"}),
        ("memory_fetch", {"ids": ["mem_cat"]}),
    ]
    assert len(model_inputs) == 3
    assert "用户家猫叫武松" in model_inputs[1][-1]["content"]
    assert "我家猫叫武松" in model_inputs[2][-1]["content"]
    assert trace["index_called"] is True
    assert trace["fetch_called"] is True
    assert trace["fetched_ids"] == ["mem_cat"]


def test_full_tool_loop_can_pull_perception_weather(monkeypatch):
    scripted = [
        {"reply": json.dumps({"tool_calls": [{"name": "perception.weather", "args": {}}]})},
        {"reply": json.dumps({"messages": ["外面下雨，记得带伞。"]})},
    ]
    model_inputs: list[list[dict]] = []

    def fake_chat_completion(runtime, messages, **kwargs):
        model_inputs.append([dict(message) for message in messages])
        return scripted.pop(0)

    monkeypatch.setattr(chat_routes.provider_client, "chat_completion", fake_chat_completion)
    monkeypatch.setattr(
        chat_routes,
        "combined_runtime_adapters_v2",
        lambda api_key, store: ToolRuntimeAdaptersV2(
            perception_pull_snapshot=lambda _user_id: {
                "condition": "rain",
                "temperature_bucket": 20,
                "is_daylight": False,
            },
        ),
    )

    result, raw_reply, trace = chat_routes._run_model_api_full_tool_loop_v2(
        object(),
        [chat_routes._model_api_full_tool_loop_instruction_message(), {"role": "user", "content": "天气怎么样？"}],
        store=types.SimpleNamespace(user_id="usr_weather"),
        api_key="key_weather",
        max_tokens=256,
        temperature=0.1,
    )

    assert json.loads(raw_reply)["messages"] == ["外面下雨，记得带伞。"]
    assert result["reply"] == raw_reply
    assert len(model_inputs) == 2
    assert "perception.weather" in model_inputs[0][0]["content"]
    assert "rain" in model_inputs[1][-1]["content"]
    assert trace["tool_calls"][0]["name"] == "perception.weather"
    assert trace["tool_calls"][0]["outcome"] == "ok"


def test_full_tool_loop_keeps_memory_index_available(monkeypatch):
    scripted = [
        {"reply": json.dumps({"tool_calls": [{"name": "memory.index", "args": {"query": "猫"}}]})},
        {"reply": json.dumps({"messages": ["记得，你家猫叫武松。"]})},
    ]
    model_inputs: list[list[dict]] = []

    def fake_chat_completion(runtime, messages, **kwargs):
        model_inputs.append([dict(message) for message in messages])
        return scripted.pop(0)

    monkeypatch.setattr(chat_routes.provider_client, "chat_completion", fake_chat_completion)
    monkeypatch.setattr(
        chat_routes,
        "combined_runtime_adapters_v2",
        lambda api_key, store: ToolRuntimeAdaptersV2(
            memory_index=lambda _user_id, _args: {"items": [{"id": "mem_cat", "summary": "用户家猫叫武松。"}]},
        ),
    )

    _result, raw_reply, trace = chat_routes._run_model_api_full_tool_loop_v2(
        object(),
        [chat_routes._model_api_full_tool_loop_instruction_message(), {"role": "user", "content": "我家猫叫什么？"}],
        store=types.SimpleNamespace(user_id="usr_memory"),
        api_key="key_memory",
        max_tokens=256,
        temperature=0.1,
    )

    assert json.loads(raw_reply)["messages"] == ["记得，你家猫叫武松。"]
    assert "用户家猫叫武松" in model_inputs[1][-1]["content"]
    assert trace["tool_calls"][0]["name"] == "memory.index"
    assert trace["tool_calls"][0]["outcome"] == "ok"


def test_full_tool_loop_slow_tool_soft_handoffs_without_inline_execution(monkeypatch):
    pull_called = False

    def fake_chat_completion(runtime, messages, **kwargs):
        return {"reply": json.dumps({"tool_calls": [{"name": "perception.steps", "args": {}}]})}

    def pull_snapshot(_user_id):
        nonlocal pull_called
        pull_called = True
        return {"step_count_bucket": 6000}

    monkeypatch.setattr(chat_routes.provider_client, "chat_completion", fake_chat_completion)
    monkeypatch.setattr(
        chat_routes,
        "combined_runtime_adapters_v2",
        lambda api_key, store: ToolRuntimeAdaptersV2(perception_pull_snapshot=pull_snapshot),
    )

    _result, raw_reply, trace = chat_routes._run_model_api_full_tool_loop_v2(
        object(),
        [chat_routes._model_api_full_tool_loop_instruction_message(), {"role": "user", "content": "今天多少步？"}],
        store=types.SimpleNamespace(user_id="usr_steps"),
        api_key="key_steps",
        max_tokens=256,
        temperature=0.1,
    )
    parsed = parse_agent_response_v2(raw_reply)

    assert parsed.needs_background is True
    assert parsed.background_request == {"tool": "perception.steps", "args": {}}
    assert trace["tool_calls"][0]["outcome"] == "needs_background"
    assert trace["tool_calls"][0]["error_code"] == "slow_budget_soft_handoff"
    assert pull_called is False
