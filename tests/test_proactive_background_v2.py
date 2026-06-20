from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from proactive.background_v2 import (
    BACKGROUND_COMPLETED,
    BackgroundWorkerV2,
    InMemoryBackgroundJobStoreV2,
)
from proactive.runtime_v2 import (
    BackgroundLeaseRegistryV2,
    RuntimeSpineV2,
    TurnOutcomeV2,
    TurnRunnerV2,
    WakeEventV2,
)


def test_background_worker_has_no_chat_or_push_adapter_and_reenters_inbox():
    spine = RuntimeSpineV2(merge_window_sec=0.0)
    store = InMemoryBackgroundJobStoreV2()
    seen_attrs = []
    job = store.create_job(
        "u1",
        {"tool": "perception.calendar"},
        turn_id="turn_1",
        wake_ids=("wake_1",),
        now=10.0,
        job_id="bg_1",
    )

    def run_background(background_job):
        seen_attrs.extend(dir(background_job))
        return {"events": [{"title": "standup"}]}

    worker = BackgroundWorkerV2(spine, store, run_background=run_background)
    result = worker.run_job("u1", job.job_id, now=10.5)
    ctx = spine.drain_context("u1", now=10.5)

    assert result.status == BACKGROUND_COMPLETED
    assert result.wake_event is not None
    assert result.wake_event.source == "background_result"
    assert not {"append_chat", "push", "send_push", "send_message"} & set(seen_attrs)
    assert ctx is not None
    assert ctx.trigger == "background_result"
    assert ctx.background_payloads[0]["background_job_id"] == "bg_1"
    assert ctx.background_payloads[0]["result"] == {"events": [{"title": "standup"}]}


def test_background_result_merges_with_newer_user_message_for_agent_arbitration():
    spine = RuntimeSpineV2(merge_window_sec=60.0)
    store = InMemoryBackgroundJobStoreV2()
    job = store.create_job(
        "u1",
        {"tool": "memory.fetch", "topic": "old question"},
        turn_id="turn_old",
        wake_ids=("wake_old",),
        now=20.0,
        job_id="bg_late",
    )
    worker = BackgroundWorkerV2(
        spine,
        store,
        run_background=lambda _job: {"answer": "old result"},
    )
    worker.run_job("u1", job.job_id, now=20.0)
    spine.submit(
        WakeEventV2(
            user_id="u1",
            source="user_message",
            trigger="user_message",
            created_at=20.1,
            latency_sensitive=True,
        )
    )
    captured = []
    runner = TurnRunnerV2(
        spine,
        recent_chat_provider=lambda _user_id: ({"role": "user", "text": "算了说点别的"},),
        run_agent=lambda context: captured.append(context) or {"actions": [{"type": "sleep", "reason": "stale_background"}]},
    )

    result = runner.run_ready_turn("u1", now=20.1)

    assert result.status == "completed"
    assert result.outcome is not None
    assert result.outcome.messages == ()
    assert captured[0]["trigger"] == "user_message"
    assert captured[0]["recent_chat"] == [{"role": "user", "text": "算了说点别的"}]
    assert captured[0]["background_payloads"][0]["background_job_id"] == "bg_late"
    assert captured[0]["background_payloads"][0]["result"] == {"answer": "old result"}


def test_foreground_turn_slot_is_free_while_background_job_is_pending():
    spine = RuntimeSpineV2(merge_window_sec=0.0)
    store = InMemoryBackgroundJobStoreV2()
    runner = TurnRunnerV2(
        spine,
        background_jobs=store,
        run_agent=lambda context: (
            {"needs_background": True, "background_request": {"tool": "memory.fetch"}}
            if context["trigger"] == "user_message" and context["wake_ids"] == ["wake_first"]
            else {"messages": ["new turn"]}
        ),
    )
    spine.submit(
        WakeEventV2(
            user_id="u1",
            source="user_message",
            trigger="user_message",
            wake_id="wake_first",
            created_at=30.0,
            latency_sensitive=True,
        )
    )

    queued = runner.run_ready_turn("u1", now=30.0)
    spine.submit(
        WakeEventV2(
            user_id="u1",
            source="user_message",
            trigger="user_message",
            wake_id="wake_second",
            created_at=30.1,
            latency_sensitive=True,
        )
    )
    second = runner.run_ready_turn("u1", now=30.1)

    assert queued.status == "background_queued"
    assert queued.background_job_id.startswith("bg_")
    assert runner.turn_leases.current("turn:u1", now=30.05) is None
    assert store.get_job("u1", queued.background_job_id) is not None
    assert second.status == "completed"
    assert second.outcome is not None
    assert second.outcome.messages == ("new turn",)


def test_background_lease_timeout_is_recoverable():
    spine = RuntimeSpineV2(merge_window_sec=0.0)
    store = InMemoryBackgroundJobStoreV2()
    leases = BackgroundLeaseRegistryV2()
    job = store.create_job("u1", {"tool": "memory.fetch"}, now=40.0, job_id="bg_reclaim")
    old_lease = leases.try_acquire_job(job.job_id, user_id="u1", owner_id="worker-a", now=40.0, ttl_sec=5.0)
    assert old_lease is not None
    assert store.mark_running("u1", job.job_id, old_lease, now=40.0) is not None

    blocked = BackgroundWorkerV2(
        spine,
        store,
        background_leases=leases,
        lease_ttl_sec=5.0,
        owner_id="worker-b",
    ).run_job("u1", job.job_id, now=44.0)
    reclaimed = BackgroundWorkerV2(
        spine,
        store,
        background_leases=leases,
        lease_ttl_sec=5.0,
        owner_id="worker-b",
        run_background=lambda _job: {"ok": True},
    ).run_job("u1", job.job_id, now=45.1)

    assert blocked.status == "busy"
    assert reclaimed.status == BACKGROUND_COMPLETED
    assert reclaimed.lease is not None
    assert reclaimed.lease.owner_id == "worker-b"
    assert store.get_job("u1", job.job_id).status == BACKGROUND_COMPLETED


def test_duplicate_background_completion_cannot_emit_second_wake():
    spine = RuntimeSpineV2(merge_window_sec=0.0)
    store = InMemoryBackgroundJobStoreV2()
    job = store.create_job("u1", {"tool": "memory.fetch"}, now=50.0, job_id="bg_once")
    worker = BackgroundWorkerV2(
        spine,
        store,
        run_background=lambda _job: {"ok": True},
    )

    first = worker.run_job("u1", job.job_id, now=50.0)
    second = worker.run_job("u1", job.job_id, now=51.0)
    ctx = spine.drain_context("u1", now=51.0)

    assert first.status == BACKGROUND_COMPLETED
    assert second.status == "not_claimed"
    assert ctx is not None
    assert ctx.trigger == "background_result"
    assert len(ctx.background_payloads) == 1
    assert ctx.background_payloads[0]["background_job_id"] == "bg_once"
