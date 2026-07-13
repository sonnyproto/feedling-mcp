"""Convergence of the resident first-greeting fix onto test's introduced_at
system (Codex review of the onboarding merge).

P0 — the fresh-resident deadlock: ``enqueue_introduction_once`` must NOT depend
on ``proactive_activation_ready`` / ``first_chat_ok_at``. A brand-new resident
who has never sent a real message must still get exactly one introduction (the
chat_loop_verified path calls this).

P1 — atomicity: claim the durable marker FIRST, append the job SECOND, and roll
the marker back if the append fails, so the one-shot marker never says
"introduced" without an actual job, and two triggers never double-send.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from agent_runtime import introduction


class FakeStore:
    """Minimal store — deliberately has NO ``proactive_activation_ready`` /
    ``first_chat_ok_at``: if enqueue_introduction_once ever consulted them this
    fake would AttributeError, which is the point (P0: it must not)."""

    def __init__(self, *, introduced=False, active=False, append="ok", append_raises=False):
        self.user_id = "usr_fake"
        self._introduced = introduced
        self._active = active
        self._append = append
        self._append_raises = append_raises
        self.appended = []
        self.unclaimed = False

    def introduction_done(self):
        return self._introduced

    def list_proactive_jobs(self, since_epoch=0, limit=0):
        return [{"job_kind": "introduction", "status": "pending"}] if self._active else []

    def claim_introduction(self, *, at_iso=None):
        if self._introduced:
            return False
        self._introduced = True
        return True

    def unclaim_introduction(self):
        self._introduced = False
        self.unclaimed = True

    def append_proactive_job(self, job):
        if self._append_raises:
            raise RuntimeError("db down")
        if not self._append:
            return None
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
    assert s.introduction_done() is True  # marker claimed


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


def test_append_exception_rolls_back_marker():
    # P1: claim succeeded, append raised -> marker rolled back, no permanent
    # "introduced" with no job.
    s = FakeStore(append_raises=True)
    with pytest.raises(RuntimeError):
        introduction.enqueue_introduction_once(s, now=1000.0)
    assert s.unclaimed is True
    assert s.introduction_done() is False


def test_append_falsy_rolls_back_marker():
    s = FakeStore(append=None)
    assert introduction.enqueue_introduction_once(s, now=1000.0) is None
    assert s.unclaimed is True
    assert s.introduction_done() is False


def test_callable_now_is_resolved():
    s = FakeStore()
    job = introduction.enqueue_introduction_once(s, now=lambda: 2500.0)
    assert job["ts"] == 2500.0
