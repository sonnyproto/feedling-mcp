from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import wake_consumer  # noqa: E402
from proactive.controls_v2 import resolve_settings_v2  # noqa: E402
from proactive.adapters_v2 import wake_event_v2_from_legacy_job  # noqa: E402
from proactive.runtime_v2 import RuntimeSpineV2, TurnRunnerV2, WakeEventV2  # noqa: E402


class FakeHostedStore:
    def __init__(self) -> None:
        self.user_id = "usr_hosted_v2"
        self.last_seen_api_key = "api-key"
        self.jobs = {"pj_1": {"job_id": "pj_1", "status": "pending"}}
        self.patches = []
        self.chat_rows = []
        self.metadata_updates = []
        self.notify_count = 0

    def load_proactive_settings(self):
        return {"enabled": True, "dnd": False, "timezone": "Asia/Shanghai"}

    def update_proactive_job(self, job_id, fields, *, only_if_status=None):
        job = self.jobs.get(job_id)
        if job is None:
            return None
        if only_if_status is not None and job.get("status") != only_if_status:
            return None
        job.update(dict(fields or {}))
        self.patches.append(dict(job))
        return dict(job)

    def append_chat(self, role, source, envelope, content_type="text", extra=None):
        row = {
            "id": envelope["id"],
            "role": role,
            "source": source,
            "extra": dict(extra or {}),
        }
        self.chat_rows.append(row)
        return row

    def notify_chat_waiters(self):
        self.notify_count += 1

    def update_chat_message_metadata(self, msg_id, fields):
        self.metadata_updates.append({"id": msg_id, "fields": dict(fields or {})})
        return self.metadata_updates[-1]


def _enable_v2(monkeypatch, enabled: bool) -> None:
    monkeypatch.setattr(
        wake_consumer.hosted_config_store,
        "_load_model_api_config",
        lambda _store: {"route": "model_api", "test_status": "ok"},
    )
    monkeypatch.setattr(
        wake_consumer.hosted_config_store,
        "_ensure_model_api_runtime_profile",
        lambda _store, _config=None: {wake_consumer.HOSTED_WAKE_RUNTIME_V2_FLAG: enabled},
    )


def _runtime_factory(reply):
    def _factory(_store, runtime, _api_key):
        spine = RuntimeSpineV2(
            settings_resolver=lambda _user_id: {"timezone": "Asia/Shanghai"},
            merge_window_sec=0.0,
        )
        runner = TurnRunnerV2(
            spine,
            run_agent=wake_consumer._hosted_wake_v2_run_agent(runtime),
        )
        return spine, runner

    return _factory


def _patch_v2_dependencies(monkeypatch, reply):
    posted_bodies = []
    push_calls = []
    monkeypatch.setattr(wake_consumer, "_hosted_wake_base_eligible", lambda _store: (True, ""))
    monkeypatch.setattr(
        wake_consumer.hosted_config_store,
        "_load_runtime_provider_config",
        lambda _store, _api_key: {"provider": "fake", "model": "fake"},
    )
    monkeypatch.setattr(
        wake_consumer.provider_client,
        "chat_completion",
        lambda *_args, **_kwargs: {"reply": json.dumps(reply)},
    )
    monkeypatch.setattr(wake_consumer, "_hosted_wake_v2_runtime", _runtime_factory(reply))
    monkeypatch.setattr(
        wake_consumer,
        "_hosted_v2_settings",
        lambda _store: resolve_settings_v2({"timezone": "Asia/Shanghai"}),
    )

    def _envelope(_store, body):
        posted_bodies.append(body.decode("utf-8"))
        return {
            "id": f"msg_{len(posted_bodies)}",
            "body_ct": "ct",
            "nonce": "nonce",
            "K_user": "key",
            "owner_user_id": _store.user_id,
        }, None

    monkeypatch.setattr(wake_consumer.core_envelope, "_build_shared_envelope_for_store", _envelope)
    monkeypatch.setattr(
        wake_consumer.push_service,
        "_deliver_ai_message_push_if_background",
        lambda *_args, **_kwargs: push_calls.append(dict(_kwargs)) or {"push_decision": "sent"},
    )
    return posted_bodies, push_calls


def test_hosted_wake_flag_off_uses_legacy_executor(monkeypatch):
    store = FakeHostedStore()
    _enable_v2(monkeypatch, False)
    calls = []
    monkeypatch.setattr(
        wake_consumer,
        "_run_model_api_wake_job_inner_legacy",
        lambda _store, _api_key, _job: calls.append((_store.user_id, _job["job_id"])),
    )

    wake_consumer._run_model_api_wake_job_inner(store, "api-key", {"job_id": "pj_1"})

    assert calls == [("usr_hosted_v2", "pj_1")]


def test_hosted_wake_flag_load_failure_falls_back_to_legacy_executor(monkeypatch):
    store = FakeHostedStore()
    monkeypatch.setattr(
        wake_consumer.hosted_config_store,
        "_load_model_api_config",
        lambda _store: (_ for _ in ()).throw(RuntimeError("profile unavailable")),
    )
    calls = []
    monkeypatch.setattr(
        wake_consumer,
        "_run_model_api_wake_job_inner_legacy",
        lambda _store, _api_key, _job: calls.append(_job["job_id"]),
    )

    wake_consumer._run_model_api_wake_job_inner(store, "api-key", {"job_id": "pj_1"})

    assert calls == ["pj_1"]


def test_hosted_wake_v2_delivers_send_message_action_via_unified_messages(monkeypatch):
    store = FakeHostedStore()
    _enable_v2(monkeypatch, True)
    posted_bodies, push_calls = _patch_v2_dependencies(
        monkeypatch,
        {
            "actions": [{"type": "send_message", "text": "visible from action"}],
        },
    )
    monkeypatch.setattr(
        wake_consumer,
        "parse_model_api_wake_actions",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy parser used")),
    )
    monkeypatch.setattr(
        wake_consumer,
        "build_model_api_wake_event_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy wake prompt used")),
    )

    wake_consumer._run_model_api_wake_job_inner(
        store,
        "api-key",
        {"job_id": "pj_1", "trigger": "heartbeat_broadcast_off", "ts": 10.0},
    )

    assert posted_bodies == ["visible from action"]
    assert push_calls
    assert store.jobs["pj_1"]["status"] == "completed"
    assert store.jobs["pj_1"]["wake_result"] == "message_sent"
    assert store.chat_rows[0]["extra"]["proactive_job_id"] == "pj_1"


def test_hosted_wake_v2_dedupes_same_text_from_messages_and_send_message_action(monkeypatch):
    store = FakeHostedStore()
    _enable_v2(monkeypatch, True)
    posted_bodies, push_calls = _patch_v2_dependencies(
        monkeypatch,
        {
            "messages": ["same text"],
            "actions": [{"type": "send_message", "text": "same text"}],
        },
    )

    wake_consumer._run_model_api_wake_job_inner(
        store,
        "api-key",
        {"job_id": "pj_1", "trigger": "heartbeat_broadcast_off", "ts": 10.0},
    )

    assert posted_bodies == ["same text"]
    assert len(push_calls) == 1
    assert store.jobs["pj_1"]["status"] == "completed"
    assert store.jobs["pj_1"]["wake_result"] == "message_sent"


def test_hosted_manual_wake_v2_requires_visible_message(monkeypatch):
    store = FakeHostedStore()
    _enable_v2(monkeypatch, True)
    posted_bodies, push_calls = _patch_v2_dependencies(
        monkeypatch,
        {"actions": [{"type": "sleep", "reason": "not_now"}]},
    )

    wake_consumer._run_model_api_wake_job_inner(
        store,
        "api-key",
        {"job_id": "pj_1", "trigger": "manual_wake", "manual": True, "ts": 10.0},
    )

    assert posted_bodies == []
    assert push_calls == []
    assert store.jobs["pj_1"]["status"] == "completed"
    assert store.jobs["pj_1"]["wake_result"] == "ignored_manual"


def test_hosted_wake_v2_delivery_off_writes_chat_without_push(monkeypatch):
    store = FakeHostedStore()
    _enable_v2(monkeypatch, True)
    posted_bodies, push_calls = _patch_v2_dependencies(
        monkeypatch,
        {"messages": ["chat only"]},
    )
    monkeypatch.setattr(
        wake_consumer,
        "_hosted_v2_settings",
        lambda _store: resolve_settings_v2({"switches": {"reminders_delivery": False}}),
    )

    wake_consumer._run_model_api_wake_job_inner(
        store,
        "api-key",
        {"job_id": "pj_1", "trigger": "heartbeat_broadcast_off", "ts": 10.0},
    )

    assert posted_bodies == ["chat only"]
    assert push_calls == []
    assert store.metadata_updates[0]["fields"]["push_decision"] == "suppressed"
    assert store.metadata_updates[0]["fields"]["push_reason"] == "reminders_delivery_disabled"


def test_scheduled_wake_compat_job_round_trips_to_v2_event():
    event = WakeEventV2(
        user_id="usr_hosted_v2",
        source="scheduled_wake",
        trigger="scheduled_wake",
        created_at=100.0,
        scheduled_note="check whether she left",
        change_digest="check whether she left",
        timezone="Asia/Shanghai",
        origin_refs=("msg_1",),
        payload={"scheduled_wake": {"wake_id": "sched_1"}},
    )

    job = wake_consumer._scheduled_event_compat_job(event)
    converted = wake_event_v2_from_legacy_job("usr_hosted_v2", job)

    assert job["status"] == "pending"
    assert job["trigger"] == "scheduled_wake"
    assert converted.source == "scheduled_wake"
    assert converted.scheduled_note == "check whether she left"
    assert converted.origin_refs == ("msg_1",)
    assert converted.payload["legacy_proactive_job"]["payload"]["v2_wake"]["scheduled_wake"]["wake_id"] == "sched_1"


def test_scheduled_transparency_compat_job_round_trips_as_background_result():
    event = WakeEventV2(
        user_id="usr_hosted_v2",
        source="background_result",
        trigger="scheduled_transparency",
        created_at=101.0,
        origin_refs=("msg_1",),
        background_payload={"type": "scheduled_wake_transparency", "reason": "scheduled_disabled"},
    )

    job = wake_consumer._scheduled_event_compat_job(event)
    converted = wake_event_v2_from_legacy_job("usr_hosted_v2", job)

    assert job["intent_label"] == "scheduled_transparency"
    assert job["trigger"] == "background_result"
    assert converted.source == "background_result"
    assert converted.trigger == "background_result"
    assert converted.background_payload["reason"] == "scheduled_disabled"
