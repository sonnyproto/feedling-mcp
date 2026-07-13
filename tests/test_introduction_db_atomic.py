"""Real-DB coverage for the cross-process exactly-once introduction enqueue
(Codex P1): the durable ``introduced_at`` marker and the proactive job are
written in ONE PostgreSQL transaction, so a second claim can never mint a
second job and the marker merge preserves peer fields.
"""
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from core import store as core_store  # noqa: E402
from chat import chat_core  # noqa: E402
from agent_runtime import introduction  # noqa: E402
import accounts.onboarding as onboarding_mod  # noqa: E402

from conftest import seed_user  # noqa: E402


def _job(now=1000.0):
    return introduction._build_introduction_job(now=now)


def _intro_jobs(store):
    return [j for j in store.list_proactive_jobs(since_epoch=0, limit=0)
            if j.get("job_kind") == "introduction"]


def test_two_claims_yield_one_job():
    uid = "usr_intro_atomic_1"
    seed_user(uid)
    s = core_store.get_store(uid)
    assert s.introduction_done() is False

    first = s.claim_and_enqueue_introduction(_job(1000.0))
    assert first is not None
    assert s.introduction_done() is True

    # Second claim loses the guarded UPSERT — no second job is minted.
    second = s.claim_and_enqueue_introduction(_job(1001.0))
    assert second is None
    assert len(_intro_jobs(s)) == 1


def test_marker_merge_preserves_peer_field():
    uid = "usr_intro_atomic_2"
    seed_user(uid)
    s = core_store.get_store(uid)
    # A peer field already lives in proactive_settings; the guarded jsonb_set
    # merge must NOT clobber it (regression guard against a whole-doc overwrite).
    s.mark_first_chat_ok(at_iso="2026-07-13T00:00:00")
    assert s.first_chat_ok_at() == "2026-07-13T00:00:00"

    assert s.claim_and_enqueue_introduction(_job()) is not None
    assert s.introduction_done() is True
    assert s.first_chat_ok_at() == "2026-07-13T00:00:00"


def test_resident_verify_helper_enqueues_once(monkeypatch):
    # End-to-end at the helper level: resident route + two verify successes
    # (double verify_loop) still yields exactly one introduction job.
    uid = "usr_intro_atomic_3"
    seed_user(uid)
    monkeypatch.setattr(onboarding_mod, "_load_onboarding_route", lambda store: "resident")
    s = core_store.get_store(uid)

    chat_core._maybe_enqueue_resident_introduction(s)
    chat_core._maybe_enqueue_resident_introduction(s)

    assert s.introduction_done() is True
    assert len(_intro_jobs(s)) == 1


def test_model_api_verify_helper_does_not_enqueue(monkeypatch):
    uid = "usr_intro_atomic_4"
    seed_user(uid)
    monkeypatch.setattr(onboarding_mod, "_load_onboarding_route", lambda store: "model_api")
    s = core_store.get_store(uid)

    chat_core._maybe_enqueue_resident_introduction(s)

    assert s.introduction_done() is False
    assert _intro_jobs(s) == []


def test_concurrent_claims_yield_exactly_one_job():
    # TRUE concurrency (two threads / two pooled connections racing the guarded
    # UPSERT), not sequential calls: exactly one wins and exactly one job lands.
    uid = "usr_intro_atomic_concurrent"
    seed_user(uid)
    s = core_store.get_store(uid)

    def _claim(i):
        return s.claim_and_enqueue_introduction(_job(1000.0 + i))

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(_claim, range(2)))

    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert s.introduction_done() is True
    assert len(_intro_jobs(s)) == 1


def test_job_insert_failure_rolls_back_marker():
    # Force the SECOND statement (job INSERT) to fail INSIDE the transaction by
    # handing it a non-JSON-serializable value, and assert the whole thing rolled
    # back: no marker, no job (Codex: model the real rollback, not FakeStore).
    uid = "usr_intro_atomic_rollback"
    seed_user(uid)
    s = core_store.get_store(uid)
    settings = dict(s.load_proactive_settings())
    at_iso = "2026-07-14T00:00:00"
    settings["introduced_at"] = at_iso
    bad_job = {"job_id": "pj_bad", "job_kind": "introduction", "bad": {1, 2, 3}}  # set -> not JSON

    result = db.claim_and_enqueue_introduction(
        uid, settings, bad_job, at_iso=at_iso, ts=1.0, item_key="pj_bad")

    assert result is None
    fresh = core_store.get_store(uid)
    assert fresh.introduction_done() is False   # marker rolled back with the failed job
    assert _intro_jobs(fresh) == []
