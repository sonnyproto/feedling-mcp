from __future__ import annotations

import sys
import uuid
import os
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from proactive.runtime_v2 import RuntimeSpineV2, TurnOutcomeV2, WakeEventV2, merge_wakes_v2
from proactive.store_v2 import (
    DBBackgroundLeaseRegistryV2,
    DBTurnLeaseRegistryV2,
    DBTurnStoreV2,
    DBWakeInboxV2,
    TURN_COMPLETED,
    TURN_STALE_RECOVERED,
    TURN_STREAM_V2,
)
import db

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 substrate tests require the PostgreSQL test fixture",
)


def _uid() -> str:
    return "usr_v2_" + uuid.uuid4().hex[:12]


def test_db_turn_lease_allows_only_one_worker_to_win():
    uid = _uid()
    leases = DBTurnLeaseRegistryV2()

    first = leases.try_acquire_user(uid, owner_id="worker-a", now=10.0, ttl_sec=30.0)
    second = leases.try_acquire_user(uid, owner_id="worker-b", now=11.0, ttl_sec=30.0)

    assert first is not None
    assert first.user_id == uid
    assert first.scope == f"turn:{uid}"
    assert second is None


def test_db_turn_lease_reclaims_expired_owner_and_rejects_old_release():
    uid = _uid()
    leases = DBTurnLeaseRegistryV2()

    first = leases.try_acquire_user(uid, owner_id="worker-a", now=10.0, ttl_sec=5.0)
    reclaimed = leases.try_acquire_user(uid, owner_id="worker-b", now=15.1, ttl_sec=5.0)

    assert first is not None
    assert reclaimed is not None
    assert reclaimed.owner_id == "worker-b"
    assert reclaimed.lease_id != first.lease_id
    assert leases.release(first) is False
    assert leases.release(reclaimed) is True
    assert leases.current(f"turn:{uid}", now=16.0) is None


def test_db_background_lease_is_independent_and_reclaimable():
    uid = _uid()
    leases = DBBackgroundLeaseRegistryV2()

    first = leases.try_acquire_job("bg_1", user_id=uid, owner_id="worker-a", now=20.0, ttl_sec=5.0)
    blocked = leases.try_acquire_job("bg_1", user_id=uid, owner_id="worker-b", now=24.0, ttl_sec=5.0)
    reclaimed = leases.try_acquire_job("bg_1", user_id=uid, owner_id="worker-b", now=25.1, ttl_sec=5.0)

    assert first is not None
    assert blocked is None
    assert reclaimed is not None
    assert reclaimed.scope == "background:bg_1"
    assert leases.release(first) is False
    assert leases.current_for_job(uid, "bg_1", now=26.0) == reclaimed


def test_db_wake_inbox_round_trips_and_merges_persisted_wakes():
    uid = _uid()
    inbox = DBWakeInboxV2()
    spine = RuntimeSpineV2(inbox=inbox, merge_window_sec=2.0)

    spine.submit(WakeEventV2(user_id=uid, source="heartbeat", trigger="heartbeat", created_at=100.0))
    spine.submit(WakeEventV2(user_id=uid, source="heartbeat", trigger="heartbeat", created_at=100.3))
    spine.submit(
        WakeEventV2(
            user_id=uid,
            source="perception_event",
            trigger="arrived_at_anchor",
            created_at=100.5,
            change_digest="anchor: cafe",
        )
    )

    assert spine.drain_context(uid, now=101.0) is None
    ctx = spine.drain_context(uid, now=103.0)

    assert ctx is not None
    assert ctx.trigger == "arrived_at_anchor"
    assert ctx.merged_triggers == ("heartbeat",)
    assert len(ctx.wake_ids) == 2
    assert "anchor: cafe" in ctx.change_digest
    assert inbox.pending_events(uid) == []


def test_db_wake_inbox_latency_sensitive_flushes_persisted_queue():
    uid = _uid()
    inbox = DBWakeInboxV2()
    spine = RuntimeSpineV2(inbox=inbox, merge_window_sec=10.0)

    spine.submit(WakeEventV2(user_id=uid, source="heartbeat", trigger="heartbeat", created_at=200.0))
    spine.submit(
        WakeEventV2(
            user_id=uid,
            source="user_message",
            trigger="user_message",
            created_at=200.2,
            latency_sensitive=True,
            manual=True,
        )
    )

    ctx = spine.drain_context(uid, now=200.3)

    assert ctx is not None
    assert ctx.trigger == "user_message"
    assert ctx.merged_triggers == ("heartbeat",)
    assert ctx.manual is True


def test_db_turn_store_recovers_stale_running_turn_without_old_owner_completion():
    uid = _uid()
    leases = DBTurnLeaseRegistryV2()
    turn_store = DBTurnStoreV2()
    context = merge_wakes_v2([
        WakeEventV2(user_id=uid, source="user_message", trigger="user_message", created_at=300.0)
    ])

    old_lease = leases.try_acquire_user(uid, owner_id="worker-a", now=300.0, ttl_sec=5.0)
    assert old_lease is not None
    stale_turn = turn_store.start_turn(uid, context, old_lease, now=300.0, turn_id="turn_stale")
    assert stale_turn is not None
    duplicate_start = turn_store.start_turn(uid, context, old_lease, now=300.1, turn_id="turn_duplicate")
    assert duplicate_start is None

    recovered = turn_store.recover_stale_running(uid, now=306.0)
    new_lease = leases.try_acquire_user(uid, owner_id="worker-b", now=306.1, ttl_sec=5.0)
    stale_start = turn_store.start_turn(uid, context, old_lease, now=306.2, turn_id="turn_stale_owner")
    old_completion = turn_store.complete_turn(
        uid,
        stale_turn.turn_id,
        old_lease,
        outcome=TurnOutcomeV2(messages=("late duplicate",)),
        delivery_committed=True,
        now=306.3,
    )

    assert len(recovered) == 1
    assert recovered[0].status == TURN_STALE_RECOVERED
    assert new_lease is not None
    assert stale_start is None
    assert old_completion is None

    new_turn = turn_store.start_turn(uid, context, new_lease, now=306.4, turn_id="turn_recovered")
    assert new_turn is not None
    completed = turn_store.complete_turn(
        uid,
        new_turn.turn_id,
        new_lease,
        outcome=TurnOutcomeV2(actions=({"type": "sleep"},)),
        now=306.5,
    )

    assert completed is not None
    assert completed.status == TURN_COMPLETED
    turns = db.log_read_all(uid, TURN_STREAM_V2)
    assert [turn["status"] for turn in turns] == [TURN_STALE_RECOVERED, TURN_COMPLETED]
