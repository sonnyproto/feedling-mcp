"""P2: resident-distill atomic claim (genesis_import_jobs status awaiting_resident).

Mirrors the CVM worker's SKIP-LOCKED claim but for the resident consumer, and must
NOT collide with the worker's `uploaded` claim.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

import db  # noqa: E402
from conftest import seed_user  # noqa: E402


def test_resident_claim_rejects_empty_consumer_id():
    # An empty consumer_id would create a resident-owned processing job that the
    # resident reaper (filters resident_consumer_id <> '') can never see → wedged.
    with pytest.raises(ValueError, match="consumer_id_required"):
        db.genesis_claim_resident_jobs("u", consumer_id="")
    with pytest.raises(ValueError, match="consumer_id_required"):
        db.genesis_claim_resident_jobs("u", consumer_id="   ")


def test_claim_resident_job_stamps_and_is_single_shot():
    uid, jid = "usr_rc_claim", "job_rc_claim_1"
    seed_user(uid)
    db.genesis_create_job(uid, {"job_id": jid, "status": "awaiting_resident"})

    claimed = db.genesis_claim_resident_jobs(uid, consumer_id="cons-A", limit=32)
    mine = [j for j in claimed if j["user_id"] == uid and j["job_id"] == jid]
    assert len(mine) == 1
    j = mine[0]
    assert j["status"] == "processing"
    assert j["resident_consumer_id"] == "cons-A"
    assert j["resident_attempts"] == 1
    assert j["resident_claimed_at"]  # timestamp set
    assert j["resident_heartbeat_at"]

    # Already processing → a second claim (even by another consumer) can't re-take it.
    again = db.genesis_claim_resident_jobs(uid, consumer_id="cons-B", limit=32)
    assert not [k for k in again if k["user_id"] == uid and k["job_id"] == jid]


def test_resident_claim_ignores_worker_uploaded_jobs():
    # A worker-lane 'uploaded' job must never be claimed by the resident consumer.
    uid, jid = "usr_rc_up", "job_rc_up_1"
    seed_user(uid)
    db.genesis_create_job(uid, {"job_id": jid, "status": "uploaded"})
    claimed = db.genesis_claim_resident_jobs(uid, consumer_id="cons-X", limit=32)
    assert not [j for j in claimed if j["user_id"] == uid and j["job_id"] == jid]
    # ...and the job is untouched (still uploaded).
    assert db.genesis_get_job(uid, jid)["status"] == "uploaded"


def test_worker_claim_ignores_resident_awaiting_jobs():
    # Symmetric: the CVM worker's 'uploaded' claim must never grab a resident job.
    uid, jid = "usr_rc_res", "job_rc_res_1"
    seed_user(uid)
    db.genesis_create_job(uid, {"job_id": jid, "status": "awaiting_resident"})
    claimed = db.genesis_claim_uploaded_jobs(limit=32)
    assert not [j for j in claimed if j["user_id"] == uid and j["job_id"] == jid]
    assert db.genesis_get_job(uid, jid)["status"] == "awaiting_resident"
