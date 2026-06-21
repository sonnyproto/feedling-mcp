import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import json
from proactive.tool_loop_v2 import run_tool_loop_v2


def test_loop_executes_tool_then_feeds_result_back_and_finishes():
    scripted = [
        '{"tool_calls": [{"name": "screen.read", "args": {"mode": "caption"}}]}',
        '{"messages": ["I see your mail inbox with 3 unread."]}',
    ]
    fed_back = []
    def call_model(messages):
        fed_back.append(messages[-1]["content"])
        return scripted.pop(0)
    tool_calls_made = []
    def call_tool(name, args):
        tool_calls_made.append((name, dict(args)))
        return {"name": name, "ok": True, "outcome": "ok",
                "result": {"caption": "Mail inbox, 3 unread"}, "error_code": "",
                "needs_background": False}

    final = run_tool_loop_v2(call_model, call_tool,
                             [{"role": "system", "content": "s"}, {"role": "user", "content": "ctx"}])
    assert tool_calls_made == [("screen.read", {"mode": "caption"})]
    assert json.loads(final)["messages"] == ["I see your mail inbox with 3 unread."]
    assert "Mail inbox, 3 unread" in fed_back[1]


def test_loop_returns_immediately_when_no_tool_calls():
    final = run_tool_loop_v2(lambda m: '{"messages": ["hello"]}',
                             lambda n, a: {}, [{"role": "system", "content": "s"}])
    assert json.loads(final)["messages"] == ["hello"]


def test_loop_caps_iterations():
    calls = {"n": 0}
    def call_model(m):
        calls["n"] += 1
        return '{"tool_calls": [{"name": "screen.read", "args": {}}]}'
    run_tool_loop_v2(call_model,
                     lambda n, a: {"name": n, "ok": True, "outcome": "ok", "result": {},
                                   "error_code": "", "needs_background": False},
                     [{"role": "system", "content": "s"}], max_iters=3)
    assert calls["n"] == 3


def test_loop_hands_off_on_needs_background():
    final = run_tool_loop_v2(
        lambda m: '{"tool_calls": [{"name": "memory.fetch", "args": {"ids": ["m1"]}}]}',
        lambda n, a: {"name": n, "ok": False, "outcome": "needs_background", "result": {},
                      "error_code": "slow_budget_soft_handoff", "needs_background": True},
        [{"role": "system", "content": "s"}])
    assert json.loads(final)["actions"][0]["type"] == "needs_background"


def test_handoff_attributes_to_first_background_tool():
    """Fix 1: when multiple tool_calls in one turn trigger needs_background,
    the handoff request must attribute to the FIRST tool, not the last."""
    reply = json.dumps({"tool_calls": [
        {"name": "tool.first", "args": {"x": 1}},
        {"name": "tool.second", "args": {"y": 2}},
    ]})
    def call_tool(name, args):
        # both tools return needs_background
        return {"name": name, "ok": False, "outcome": "needs_background", "result": {},
                "error_code": "slow_budget_soft_handoff", "needs_background": True}

    final = run_tool_loop_v2(lambda m: reply, call_tool, [{"role": "system", "content": "s"}])
    parsed = json.loads(final)
    handoff_request = parsed["actions"][0]["request"]
    assert handoff_request["tool"] == "tool.first", (
        f"Expected first tool to be attributed but got: {handoff_request['tool']}"
    )


def test_loop_stops_executing_remaining_calls_after_handoff():
    """Once a tool in the turn signals needs_background, the loop must NOT run
    the rest of that turn's calls — deferring inline work (and, on resident, an
    HTTP round-trip per call) is the whole point of the handoff."""
    reply = json.dumps({"tool_calls": [
        {"name": "tool.first", "args": {}},
        {"name": "tool.second", "args": {}},
        {"name": "tool.third", "args": {}},
    ]})
    invoked = []
    def call_tool(name, args):
        invoked.append(name)
        if name == "tool.first":
            return {"name": name, "ok": False, "outcome": "needs_background", "result": {},
                    "error_code": "slow_budget_soft_handoff", "needs_background": True}
        return {"name": name, "ok": True, "outcome": "ok", "result": {},
                "error_code": "", "needs_background": False}

    final = run_tool_loop_v2(lambda m: reply, call_tool, [{"role": "system", "content": "s"}])
    assert invoked == ["tool.first"], f"only the first call should run; got {invoked}"
    assert json.loads(final)["actions"][0]["type"] == "needs_background"
