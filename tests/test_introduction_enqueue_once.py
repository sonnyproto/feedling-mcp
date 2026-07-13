"""Convergence of the resident first-greeting fix onto test's introduced_at
system (Codex review of the onboarding merge).

P0 — the fresh-resident deadlock: ``enqueue_introduction_once`` must NOT depend
on ``proactive_activation_ready`` / ``first_chat_ok_at``. A brand-new resident
who has never sent a real message must still get exactly one introduction (the
chat_loop_verified path calls this).

P1 — exactly-once is enforced by ``store.claim_and_enqueue_introduction`` (a
single DB transaction: guarded marker UPSERT + job INSERT). This file unit-tests
the ``enqueue_introduction_once`` wiring against that contract with a fake store;
the real cross-process / rollback transaction is covered in
``test_introduction_db_atomic.py``.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from agent_runtime import introduction


class FakeStore:
    """Minimal store — deliberately has NO ``proactive_activation_ready`` /
    ``first_chat_ok_at``: if enqueue_introduction_once ever consulted them this
    fake would AttributeError, which is the point (P0: it must not).

    ``claim_and_enqueue_introduction`` models the DB transaction's contract:
    won -> marker set + job stored + returns job; lost/rolled-back -> returns
    None and the marker is unchanged (never "introduced" without a job)."""

    def __init__(self, *, introduced=False, active=False, txn_rolls_back=False):
        self.user_id = "usr_fake"
        self._introduced = introduced
        self._active = active
        self._txn_rolls_back = txn_rolls_back
        self.appended = []

    def introduction_done(self):
        return self._introduced

    def list_proactive_jobs(self, since_epoch=0, limit=0):
        return [{"job_kind": "introduction", "status": "pending"}] if self._active else []

    def claim_and_enqueue_introduction(self, job):
        if self._introduced:          # lost the race / already introduced
            return None
        if self._txn_rolls_back:      # job INSERT failed -> transaction rolled marker back
            return None               # marker stays empty
        self._introduced = True
        self.appended.append(job)
        return job


def test_fresh_resident_enqueues_one_without_activation_ready():
    # P0: no first_chat_ok / proactive_activation_ready anywhere — still enqueues.
    s = FakeStore()
    job = introduction.enqueue_introduction_once(s, now=1000.0)
    assert job is not None
    assert job["job_kind"] == "introduction"
    assert job["trigger"] == "post_spawn_genesis"
    assert len(s.appended) == 1
    assert s.introduction_done() is True


def test_already_introduced_is_noop():
    s = FakeStore(introduced=True)
    assert introduction.enqueue_introduction_once(s, now=1000.0) is None
    assert s.appended == []


def test_active_job_in_flight_is_noop():
    s = FakeStore(active=True)
    assert introduction.enqueue_introduction_once(s, now=1000.0) is None
    assert s.appended == []


def test_second_call_loses_claim_no_double_send():
    # Two triggers (supervisor + chat_loop_verified) racing: only ONE enqueues.
    s = FakeStore()
    first = introduction.enqueue_introduction_once(s, now=1000.0)
    second = introduction.enqueue_introduction_once(s, now=1001.0)
    assert first is not None and second is None
    assert len(s.appended) == 1


def test_rolled_back_transaction_leaves_no_marker():
    # P1: the DB transaction rolled back (job INSERT failed) -> None AND the
    # marker is NOT set, so a later trigger can still introduce.
    s = FakeStore(txn_rolls_back=True)
    assert introduction.enqueue_introduction_once(s, now=1000.0) is None
    assert s.introduction_done() is False
    assert s.appended == []


def test_callable_now_is_resolved():
    s = FakeStore()
    job = introduction.enqueue_introduction_once(s, now=lambda: 2500.0)
    assert job["ts"] == 2500.0
