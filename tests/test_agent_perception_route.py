from __future__ import annotations

import sys
from pathlib import Path

from flask import Flask

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from agent import routes as agent_routes  # noqa: E402


def _client(monkeypatch, *, settings=None, state=None, snapshot=None, pull=None):
    class Store:
        user_id = "u_agent"

        def load_proactive_settings(self):
            return dict(settings or {})

    monkeypatch.setattr(agent_routes.auth, "require_user", lambda: Store())
    monkeypatch.setattr(agent_routes.perception_store, "get_state", lambda uid: dict(state or {}))
    monkeypatch.setattr(agent_routes.perception_service, "snapshot", lambda uid: dict(snapshot or {}))
    monkeypatch.setattr(agent_routes.perception_service, "pull_snapshot", lambda uid: dict(pull or {}))

    app = Flask("agent-route-test")
    app.register_blueprint(agent_routes.bp)
    return app.test_client()


def test_agent_perception_returns_requested_fast_signals(monkeypatch):
    client = _client(
        monkeypatch,
        snapshot={
            "local_time": "2026-06-23T12:00:00+08:00",
            "timezone": "Asia/Shanghai",
            "locale": "zh-Hans-CN",
            "battery_level": 0.72,
            "charging": False,
            "place_label": "home",
            "motion_state": "still",
            "now_playing": {"title": "Song"},
            "broadcast_state": "off",
            "broadcast_active": False,
            "user_state": "default",
        },
        pull={
            "place_label": "home",
            "wifi_label": "home_wifi",
            "country": "CN",
            "locality": "深圳市",
            "wifi_anchor_id": "wifi-home",
            "condition": "rain",
            "temperature": 23.4,
            "is_daylight": True,
        },
    )

    resp = client.get("/v1/agent/perception?signals=now,weather,location")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["signals"]["now"]["time"] == "2026-06-23T12:00:00+08:00"
    assert body["signals"]["now"]["battery_level"] == 0.72
    assert body["signals"]["weather"] == {
        "condition": "rain",
        "temperature": 23.4,
        "is_daylight": True,
    }
    assert body["signals"]["location"]["locality"] == "深圳市"
    assert body["signals"]["location"]["wifi_anchor_id"] == "wifi-home"


def test_agent_perception_calendar_returns_event_list(monkeypatch):
    calendar_events = [
        {
            "title": "Yesterday review",
            "next_event_time": "2026-06-22T09:00:00+08:00",
            "end_time": "2026-06-22T09:30:00+08:00",
            "event_kind": "meeting",
            "attendee_count": 2,
            "is_all_day": False,
            "duration_min": 30,
            "minutes_until_start": -1500,
        },
        {
            "title": "1:1",
            "next_event_time": "2026-06-23T10:00:00+08:00",
            "end_time": "2026-06-23T10:30:00+08:00",
            "event_kind": "meeting",
            "attendee_count": 2,
            "is_all_day": False,
            "duration_min": 30,
            "minutes_until_start": 25,
        },
    ]
    client = _client(
        monkeypatch,
        pull={
            "calendar_next_event": calendar_events[1],
            "calendar_events": calendar_events,
            "calendar_events_truncated": False,
        },
    )

    resp = client.get("/v1/agent/perception?signals=calendar")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["signals"]["calendar"] == {
        "calendar_next_event": calendar_events[1],
        "calendar_events": calendar_events,
        "calendar_events_truncated": False,
    }


def test_agent_perception_slow_signals_return_inline_without_background(monkeypatch):
    client = _client(
        monkeypatch,
        pull={
            "step_count": 6500,
            "asleep_minutes": 420,
            "workout_type": "run",
            "duration_min": 30,
            "count_today": 1,
            "resting_heart_rate": 60,
        },
    )

    resp = client.get("/v1/agent/perception?signals=steps,sleep,workout,vitals")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["signals"]["steps"] == {"step_count": 6500}
    assert body["signals"]["sleep"] == {"asleep_minutes": 420}
    assert body["signals"]["workout"] == {
        "workout_type": "run",
        "duration_min": 30,
        "count_today": 1,
    }
    assert body["signals"]["vitals"] == {
        "resting_heart_rate": 60,
        "step_count": 6500,
    }
    assert "needs_background" not in str(body)


def test_agent_perception_pull_only_signals_return_inline(monkeypatch):
    client = _client(
        monkeypatch,
        pull={
            "focus_authorization_status": "authorized",
            "in_focus": True,
            "output_type": "bluetooth",
            "is_bluetooth": True,
            "device_name": "AirPods",
        },
    )

    resp = client.get("/v1/agent/perception?signals=focus,audio_route")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["signals"]["focus"] == {
        "focus_authorization_status": "authorized",
        "in_focus": True,
    }
    assert body["signals"]["audio_route"] == {
        "output_type": "bluetooth",
        "is_bluetooth": True,
        "device_name": "AirPods",
    }


def test_agent_perception_pull_only_null_permission_messages_return_disabled(monkeypatch):
    client = _client(
        monkeypatch,
        state={
            "focus_authorization_status": {
                "v": None,
                "ts": 10.0,
                "msg": "专注模式未授权",
            },
            "output_type": {
                "v": None,
                "ts": 10.0,
                "msg": "audio route permission denied",
            },
        },
        pull={
            "focus_authorization_status": None,
            "in_focus": None,
            "output_type": None,
            "is_bluetooth": None,
            "device_name": None,
        },
    )

    resp = client.get("/v1/agent/perception?signals=focus,audio_route")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["signals"]["focus"] == {"disabled": True, "reason": "not_permitted"}
    assert body["signals"]["audio_route"] == {"disabled": True, "reason": "not_permitted"}


def test_agent_perception_permission_states_return_disabled(monkeypatch):
    client = _client(
        monkeypatch,
        settings={
            "permission_states": {
                "weather": "off",
                "location": "not_permitted",
                "focus": "off",
                "audio_route": "not_permitted",
            }
        },
        pull={
            "condition": "rain",
            "place_label": "home",
            "focus_authorization_status": "authorized",
            "in_focus": True,
            "output_type": "bluetooth",
        },
    )

    resp = client.get("/v1/agent/perception?signals=weather,location,focus,audio_route")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["signals"]["weather"] == {"disabled": True, "reason": "switch_off"}
    assert body["signals"]["location"] == {"disabled": True, "reason": "not_permitted"}
    assert body["signals"]["focus"] == {"disabled": True, "reason": "switch_off"}
    assert body["signals"]["audio_route"] == {"disabled": True, "reason": "not_permitted"}


def test_agent_perception_null_permission_message_returns_disabled_but_no_event_does_not(monkeypatch):
    client = _client(
        monkeypatch,
        state={
            "asleep_minutes": {
                "v": None,
                "ts": 10.0,
                "msg": "HealthKit 不可用，无法读取睡眠趋势",
            },
            "calendar_next_event": {
                "v": None,
                "ts": 10.0,
                "msg": "未来 24 小时内没有可用日程",
            },
        },
        pull={
            "asleep_minutes": None,
            "calendar_next_event": None,
        },
    )

    resp = client.get("/v1/agent/perception?signals=sleep,calendar")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["signals"]["sleep"] == {"disabled": True, "reason": "not_permitted"}
    assert body["signals"]["calendar"] == {
        "calendar_next_event": None,
        "calendar_events": None,
        "calendar_events_truncated": None,
    }


def test_agent_perception_rejects_unknown_signals(monkeypatch):
    client = _client(monkeypatch)

    resp = client.get("/v1/agent/perception?signals=now,nope")
    body = resp.get_json()

    assert resp.status_code == 400
    assert body["ok"] is False
    assert body["error"] == "unknown_signals"
    assert body["unknown"] == ["nope"]
