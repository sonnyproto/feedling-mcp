from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from accounts import registry  # noqa: E402
from admin import admin_core, data_track  # noqa: E402
from core.reqctx import bind  # noqa: E402


def _event(ts: float, user_id: str, typ: str, *, trace_id: str, status: str = "ok", **extra) -> dict:
    return {
        "ts": ts,
        "user_id": user_id,
        "subsystem": typ.split(".", 1)[0],
        "type": typ,
        "actor": "backend",
        "status": status,
        "summary": extra.get("summary", ""),
        "explain": extra.get("explain", typ),
        "trace_id": trace_id,
        "turn_id": trace_id,
        "job_id": "",
        "dur_ms": extra.get("dur_ms"),
        "detail": extra.get("detail", {}),
        "content_excerpt": extra.get("content_excerpt", {}),
    }


def test_debug_payload_groups_multi_user_trace_and_marks_stalled(monkeypatch):
    with registry._users_lock:
        registry._users[:] = [
            {"user_id": "user_a", "principal_id": "p_a", "created_at": "2026-07-04T00:00:00Z"},
            {"user_id": "user_b", "principal_id": "p_b", "created_at": "2026-07-04T00:00:00Z"},
        ]

    blobs = {
        ("user_a", "v1_flow_trace_enabled"): {"enabled": True},
        ("user_b", "v1_flow_trace_enabled"): {"enabled": True},
        ("user_a", "v1_flow_trace"): {
            "events": [
                _event(100, "user_a", "route.chat.message", trace_id="t-ok"),
                _event(101, "user_a", "agent.model.call.start", trace_id="t-ok"),
                _event(
                    104,
                    "user_a",
                    "agent.model.call.done",
                    trace_id="t-ok",
                    dur_ms=3000,
                    detail={"input_tokens": 12, "output_tokens": 34},
                    content_excerpt={"reply": "hello from model"},
                ),
            ]
        },
        ("user_b", "v1_flow_trace"): {
            "events": [
                _event(200, "user_b", "route.chat.message", trace_id="t-stall"),
                _event(
                    201,
                    "user_b",
                    "agent.model.call.start",
                    trace_id="t-stall",
                    content_excerpt={"prompt_head": "private beta prompt"},
                ),
            ]
        },
    }

    monkeypatch.setattr(data_track.db, "get_blob", lambda uid, kind: blobs.get((uid, kind)))

    with bind("view=debug"):
        payload = data_track._data_track_debug_payload()

    assert payload["summary"]["users_with_events"] == 2
    assert payload["summary"]["events_total"] == 5
    assert [u["user_id"] for u in payload["users"]] == ["user_b", "user_a"]

    turns = {turn["trace_id"]: turn for turn in payload["turns"]}
    assert turns["t-stall"]["terminal_status"] == "stalled"
    assert turns["t-stall"]["is_stalled"] is True
    assert turns["t-ok"]["terminal_status"] == "ok"
    assert turns["t-ok"]["total_dur_ms"] == 3000


def test_debug_page_renders_nav_filters_and_plaintext_excerpt(monkeypatch):
    with registry._users_lock:
        registry._users[:] = [{"user_id": "user_a", "principal_id": "p_a"}]

    blobs = {
        ("user_a", "v1_flow_trace_enabled"): {"enabled": True},
        ("user_a", "v1_flow_trace"): {
            "events": [
                _event(
                    100,
                    "user_a",
                    "agent.reply",
                    trace_id="t-reply",
                    content_excerpt={"reply": "visible beta reply"},
                )
            ]
        },
    }
    monkeypatch.setattr(data_track.db, "get_blob", lambda uid, kind: blobs.get((uid, kind)))

    html = admin_core.page_html("view=debug&user_id=user_a&q=reply")

    assert "Debug" in html
    assert "user_a" in html
    assert "agent.reply" in html
    assert "visible beta reply" in html
    assert "Filter debug logs" in html
