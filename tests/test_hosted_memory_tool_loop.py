from __future__ import annotations

import json
import sys
import types
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import chat_routes  # noqa: E402


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
