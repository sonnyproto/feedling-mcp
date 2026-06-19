from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from proactive.tool_catalog_v2 import (
    FAST,
    SLOW,
    default_tool_catalog_v2,
    tool_catalog_v2_for_runtime,
)
from proactive.tool_executor_v2 import (
    ToolBudgetV2,
    ToolCallV2,
    ToolExecutorV2,
    ToolRuntimeAdaptersV2,
)


def _adapters(*, send_message=None, photos_recent=None) -> ToolRuntimeAdaptersV2:
    snapshot = {
        "place_label": "home",
        "wifi_label": "wifi-home",
        "country": "US",
        "calendar_next_event": {"title": "Dentist", "starts_at": "2026-06-20T10:00:00+08:00"},
        "now_playing": {"title": "Song"},
        "motion_state": "walking",
    }
    memories = [
        {
            "id": "mem_1",
            "type": "fact",
            "title": "Likes quiet cafes",
            "summary": "The user likes quiet cafes.",
            "occurred_at": "2026-01-01",
        },
        {
            "id": "mem_2",
            "type": "event",
            "title": "Hospital visit",
            "summary": "Had a hospital appointment.",
            "occurred_at": "2026-06-01",
        },
    ]
    return ToolRuntimeAdaptersV2(
        perception_snapshot=lambda _user_id: snapshot,
        photos_recent=photos_recent or (lambda _user_id, limit: {"photos": [{"photo_id": "p1"}], "limit": limit}),
        memory_load=lambda _user_id: memories,
        send_message=send_message,
    )


def test_hosted_and_resident_derive_from_same_catalog_source():
    hosted = tool_catalog_v2_for_runtime("hosted")
    resident = tool_catalog_v2_for_runtime("resident")

    assert hosted.signature() == resident.signature()
    assert hosted.context_tools() == resident.context_tools()


def test_each_tool_has_stable_cost_class_and_expected_thresholds():
    catalog = default_tool_catalog_v2()

    for spec in catalog.specs():
        assert spec.cost_class in {FAST, SLOW}
        assert catalog.cost_class_for(spec.name) in {FAST, SLOW}

    assert catalog.cost_class_for("perception.calendar", {"window_days": 7}) == FAST
    assert catalog.cost_class_for("perception.calendar", {"window_days": 8}) == SLOW
    assert catalog.cost_class_for("screen.read", {"mode": "caption"}) == FAST
    assert catalog.cost_class_for("screen.read", {"mode": "full"}) == SLOW


def test_executor_runs_minimum_available_tools_with_injected_action_adapter():
    sent = []
    executor = ToolExecutorV2(
        adapters=_adapters(send_message=lambda user_id, text, args: sent.append((user_id, text, dict(args))) or {"message_id": "m1"}),
        budget=ToolBudgetV2(fast_hard_limit=12, slow_inline_limit=2),
    )

    calls = [
        ToolCallV2("perception.now", user_id="u1"),
        ToolCallV2("perception.location", user_id="u1"),
        ToolCallV2("perception.calendar", user_id="u1", args={"window_days": 1}),
        ToolCallV2("perception.now_playing", user_id="u1"),
        ToolCallV2("perception.motion", user_id="u1"),
        ToolCallV2("perception.photo_recent", user_id="u1", args={"limit": 1}),
        ToolCallV2("memory.index", user_id="u1"),
        ToolCallV2("memory.fetch", user_id="u1", args={"ids": ["mem_2"]}),
        ToolCallV2("send_message", user_id="u1", args={"text": "hello"}),
        ToolCallV2("sleep", user_id="u1", args={"reason": "not_now"}),
    ]
    results = [executor.execute(call) for call in calls]

    assert all(result.ok for result in results)
    assert results[0].result["snapshot"]["place_label"] == "home"
    assert results[1].result["location"]["wifi_label"] == "wifi-home"
    assert results[2].result["calendar_next_event"]["title"] == "Dentist"
    assert results[5].result["photos"][0]["photo_id"] == "p1"
    assert results[6].result["memories"][0]["id"] == "mem_1"
    assert results[7].result["memories"][0]["id"] == "mem_2"
    assert sent == [("u1", "hello", {"text": "hello"})]


def test_healthkit_tools_fail_explicitly_without_fake_data():
    executor = ToolExecutorV2(adapters=_adapters())

    for name in ("perception.steps", "perception.sleep_last_night", "perception.workout", "perception.vitals"):
        result = executor.execute(ToolCallV2(name, user_id="u1", wake_id="wake_1", turn_id="turn_1"))
        assert result.ok is False
        assert result.outcome == "unavailable"
        assert result.error_code == "healthkit_unavailable"
        assert result.result == {}
        assert result.trace is not None
        assert result.trace.name == name
        assert result.trace.wake_id == "wake_1"
        assert result.trace.turn_id == "turn_1"


def test_tool_traces_record_name_cost_latency_outcome_and_wake_turn_ids():
    traces = []
    executor = ToolExecutorV2(adapters=_adapters(), trace_sink=traces.append)

    result = executor.execute(
        ToolCallV2("perception.now", user_id="u1", wake_id="wake_trace", turn_id="turn_trace")
    )

    assert result.ok is True
    assert result.trace is not None
    trace = result.trace
    assert trace.name == "perception.now"
    assert trace.cost_class == FAST
    assert trace.outcome == "ok"
    assert trace.latency_ms >= 0.0
    assert trace.wake_id == "wake_trace"
    assert trace.turn_id == "turn_trace"
    assert traces == [trace]
    assert executor.traces == [trace]


def test_fast_slow_budget_returns_soft_handoff_not_silent_truncation():
    photo_calls = []
    executor = ToolExecutorV2(
        adapters=_adapters(photos_recent=lambda _user_id, limit: photo_calls.append(limit) or {"photos": []}),
        budget=ToolBudgetV2(fast_hard_limit=2, slow_inline_limit=1),
    )

    first_slow = executor.execute(ToolCallV2("memory.fetch", user_id="u1", args={"ids": ["mem_1"]}))
    second_slow = executor.execute(ToolCallV2("perception.photo_recent", user_id="u1"))
    first_fast = executor.execute(ToolCallV2("perception.now", user_id="u1"))
    second_fast = executor.execute(ToolCallV2("perception.motion", user_id="u1"))
    third_fast = executor.execute(ToolCallV2("perception.location", user_id="u1"))

    assert first_slow.ok is True
    assert second_slow.ok is False
    assert second_slow.outcome == "needs_background"
    assert second_slow.needs_background is True
    assert second_slow.error_code == "slow_budget_soft_handoff"
    assert photo_calls == []
    assert first_fast.ok is True
    assert second_fast.ok is True
    assert third_fast.ok is False
    assert third_fast.outcome == "needs_background"
    assert third_fast.error_code == "fast_budget_soft_handoff"


def test_send_message_without_output_adapter_fails_explicitly():
    executor = ToolExecutorV2(adapters=_adapters(send_message=None))

    result = executor.execute(ToolCallV2("send_message", user_id="u1", args={"text": "hello"}))

    assert result.ok is False
    assert result.outcome == "unavailable"
    assert result.error_code == "send_message_adapter_missing"


def test_unavailable_tools_are_not_masked_by_budget_handoff():
    executor = ToolExecutorV2(adapters=_adapters(), budget=ToolBudgetV2(slow_inline_limit=0))

    healthkit = executor.execute(ToolCallV2("perception.steps", user_id="u1"))
    screen = executor.execute(ToolCallV2("screen.read", user_id="u1", args={"mode": "full"}))

    assert healthkit.outcome == "unavailable"
    assert healthkit.error_code == "healthkit_unavailable"
    assert screen.outcome == "unavailable"
    assert screen.error_code == "tool_not_implemented_in_pr3"
