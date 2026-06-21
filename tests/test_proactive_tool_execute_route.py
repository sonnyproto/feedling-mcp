"""Integration tests for POST /v1/proactive/tool/execute.

These tests import ``app`` and therefore need Postgres (see conftest.py).
They must NOT be added to conftest._PURE_UNIT.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import json
import app as app_module


def _client():
    return app_module.app.test_client()


def test_tool_execute_runs_a_tool(monkeypatch):
    # Monkeypatch auth.require_user to bypass real auth, and
    # combined_runtime_adapters_v2 to bypass the enclave.
    from proactive import routes as proactive_routes
    from proactive.tool_executor_v2 import ToolRuntimeAdaptersV2

    class _Store:
        user_id = "u1"
        last_seen_api_key = "k"

    monkeypatch.setattr(proactive_routes.auth, "require_user", lambda: _Store())
    monkeypatch.setattr(
        proactive_routes, "combined_runtime_adapters_v2",
        lambda api_key, store: ToolRuntimeAdaptersV2(
            screen_read=lambda uid, fid, mode: {"frame_id": "f1", "caption": "Inbox", "mode": mode}))

    resp = _client().post("/v1/proactive/tool/execute",
                          json={"name": "screen.read", "args": {"mode": "caption"}})
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["result"]["caption"] == "Inbox"


def test_tool_execute_unknown_tool_is_error(monkeypatch):
    from proactive import routes as proactive_routes
    from proactive.tool_executor_v2 import ToolRuntimeAdaptersV2

    class _Store:
        user_id = "u1"
        last_seen_api_key = "k"

    monkeypatch.setattr(proactive_routes.auth, "require_user", lambda: _Store())
    monkeypatch.setattr(proactive_routes, "combined_runtime_adapters_v2",
                        lambda api_key, store: ToolRuntimeAdaptersV2())
    resp = _client().post("/v1/proactive/tool/execute", json={"name": "no.such.tool", "args": {}})
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is False
    assert body["error_code"] == "unknown_tool"


def test_tool_execute_foreground_budget_soft_handoffs_slow_tool(monkeypatch):
    from proactive import routes as proactive_routes
    from proactive.tool_executor_v2 import ToolRuntimeAdaptersV2

    class _Store:
        user_id = "u1"
        last_seen_api_key = "k"

    called = {"pull": False}

    def pull_snapshot(_user_id):
        called["pull"] = True
        return {"step_count_bucket": 6000}

    monkeypatch.setattr(proactive_routes.auth, "require_user", lambda: _Store())
    monkeypatch.setattr(
        proactive_routes,
        "combined_runtime_adapters_v2",
        lambda api_key, store: ToolRuntimeAdaptersV2(perception_pull_snapshot=pull_snapshot),
    )

    resp = _client().post(
        "/v1/proactive/tool/execute",
        json={"name": "perception.steps", "args": {}, "budget_mode": "foreground_chat_fast"},
    )
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["ok"] is False
    assert body["needs_background"] is True
    assert body["outcome"] == "needs_background"
    assert body["error_code"] == "slow_budget_soft_handoff"
    assert called["pull"] is False
