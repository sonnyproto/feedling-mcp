from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from perception.differ_v2 import PerceptionDifferV2
from proactive.adapters_v2 import wake_event_v2_from_legacy_job
from proactive.runtime_v2 import RuntimeSpineV2, WakeEventV2, merge_wakes_v2
from proactive.tool_catalog_v2 import FAST, SLOW, default_tool_catalog_v2


def test_wake_inbox_merges_nearby_wakes_and_dedupes_same_trigger():
    spine = RuntimeSpineV2(merge_window_sec=2.0)
    spine.submit(WakeEventV2(user_id="u1", source="heartbeat", trigger="heartbeat", created_at=100.0))
    spine.submit(WakeEventV2(user_id="u1", source="heartbeat", trigger="heartbeat", created_at=100.2))
    spine.submit(
        WakeEventV2(
            user_id="u1",
            source="perception_event",
            trigger="arrived_at_anchor",
            created_at=100.5,
            change_digest="anchor: home -> work",
        )
    )

    assert spine.drain_context("u1", now=101.0) is None
    ctx = spine.drain_context("u1", now=103.0)

    assert ctx is not None
    assert ctx.trigger == "arrived_at_anchor"
    assert ctx.merged_triggers == ("heartbeat",)
    assert len(ctx.wake_ids) == 2
    assert "anchor: home -> work" in ctx.change_digest
    assert any(tool["name"] == "perception.now" for tool in ctx.tools)


def test_latency_sensitive_wake_flushes_without_waiting_for_window():
    spine = RuntimeSpineV2(merge_window_sec=3.0)
    spine.submit(WakeEventV2(user_id="u1", source="heartbeat", trigger="heartbeat", created_at=200.0))
    spine.submit(
        WakeEventV2(
            user_id="u1",
            source="user_message",
            trigger="user_message",
            created_at=200.2,
            latency_sensitive=True,
            manual=True,
        )
    )

    ctx = spine.drain_context("u1", now=200.25)

    assert ctx is not None
    assert ctx.trigger == "user_message"
    assert ctx.latency_sensitive is True
    assert ctx.manual is True
    assert ctx.merged_triggers == ("heartbeat",)


def test_background_result_is_context_not_direct_chat_write():
    ctx = merge_wakes_v2([
        WakeEventV2(
            user_id="u1",
            source="background_result",
            trigger="background_result",
            background_payload={"tool": "perception.calendar", "result": {"events": 2}},
        )
    ])

    turn_context = ctx.as_turn_context()
    assert turn_context["trigger"] == "background_result"
    assert turn_context["background_payloads"] == [
        {"tool": "perception.calendar", "result": {"events": 2}}
    ]


def test_tool_catalog_cost_classes_and_pull_only_motion():
    catalog = default_tool_catalog_v2()

    assert catalog.cost_class_for("perception.now") == FAST
    assert catalog.cost_class_for("screen.read", {"mode": "caption"}) == FAST
    assert catalog.cost_class_for("screen.read", {"mode": "full"}) == SLOW
    assert catalog.cost_class_for("perception.calendar", {"window_days": 7}) == FAST
    assert catalog.cost_class_for("perception.calendar", {"window_days": 30}) == SLOW
    assert catalog.get("perception.motion").wake_source is False


def test_perception_differ_only_discrete_anchor_wakes_location():
    differ = PerceptionDifferV2()

    motion = differ.observe("u1", "motion_state", "walking", ts=1.0)
    place = differ.observe("u1", "place_label", "home", ts=2.0)
    anchor = differ.observe("u1", "connectivity_anchor", {"anchor_id": "wifi-home", "label": "home"}, ts=3.0)

    assert motion.events == ()
    assert place.events == ()
    assert len(anchor.events) == 1
    assert anchor.events[0].source == "perception_event"
    assert anchor.events[0].trigger == "arrived_at_anchor"
    assert anchor.presence_hints["entered_anchor"] == "home"


def test_perception_differ_tracks_seen_separately_from_changed():
    differ = PerceptionDifferV2()

    first = differ.observe("u1", "connectivity_anchor", "wifi-home", ts=10.0)
    second = differ.observe("u1", "connectivity_anchor", "wifi-home", ts=20.0)

    assert first.state.last_seen_ts == 10.0
    assert first.state.last_changed_ts == 10.0
    assert second.changed is False
    assert second.state.last_seen_ts == 20.0
    assert second.state.last_changed_ts == 10.0
    assert second.events == ()


def test_legacy_job_adapter_keeps_old_shape_at_boundary():
    event = wake_event_v2_from_legacy_job(
        "u1",
        {
            "job_id": "pj_1",
            "wake_id": "wake_1",
            "trigger": "perception_location",
            "ts": 123.0,
            "context_hint": "legacy hint",
            "frame_ids": ["frame_1"],
        },
    )

    assert event.source == "perception_event"
    assert event.trigger == "perception_location"
    assert event.change_digest == "legacy hint"
    assert event.payload["legacy_proactive_job"]["job_id"] == "pj_1"
