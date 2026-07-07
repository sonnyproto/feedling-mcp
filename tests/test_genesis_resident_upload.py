"""P2 request-layer: resident sealed upload branch (store ciphertext + awaiting_resident job).

DB-verifiable: size limit, job creation in awaiting_resident, ciphertext stored + roundtrips,
idempotent re-upload. (The enclave/AAD crypto correctness is NOT verified here — real VPS e2e.)
"""
import base64
import hashlib
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent))

from genesis import genesis_core  # noqa: E402
import db  # noqa: E402
from conftest import seed_user  # noqa: E402


def _sealed_body(plaintext: bytes, *, client_job_id="cj-1", mode="add_memory"):
    ct = plaintext  # stand-in ciphertext (real client seals; server stores bytes as-is)
    return {
        "format": "sealed_v1",
        "client_job_id": client_job_id,
        "mode": mode,
        "ciphertext_b64": base64.b64encode(ct).decode("ascii"),
        "ciphertext_sha256": hashlib.sha256(ct).hexdigest(),
        "content_sha256": hashlib.sha256(plaintext).hexdigest(),
        "aad": {"owner_user_id": "u", "v": 1, "item_id": "it"},
    }


def _import(uid, payload):
    return genesis_core._resident_sealed_import(types.SimpleNamespace(user_id=uid), payload)


def test_resident_upload_stores_ciphertext_and_creates_awaiting_job(monkeypatch):
    monkeypatch.delenv("FEEDLING_RESIDENT_DISTILL_MAX_BYTES", raising=False)
    uid = "usr_res_up_ok"
    seed_user(uid)
    ct = b"sealed-material-bytes-xyz"
    body, status = _import(uid, _sealed_body(ct))
    assert status == 200
    jid = body["job"]["job_id"]
    assert body["job"]["status"] == "processing"       # app-facing status, not awaiting_resident

    job = db.genesis_get_job(uid, jid)
    assert job["status"] == "awaiting_resident"          # internal status → claimable
    chunks = db.genesis_list_chunks(uid, jid)
    assert len(chunks) == 1
    assert chunks[0]["encrypted_body"] == ct             # ciphertext stored + roundtrips


def test_resident_upload_rejects_incomplete_envelope():
    uid = "usr_res_up_bad"
    seed_user(uid)
    body, status = _import(uid, {"format": "sealed_v1", "client_job_id": "x"})  # no ciphertext/aad
    assert status == 400
    assert body["error"] == "sealed_envelope_incomplete"


def test_resident_upload_enforces_size_limit(monkeypatch):
    monkeypatch.setenv("FEEDLING_RESIDENT_DISTILL_MAX_BYTES", "16")
    uid = "usr_res_up_big"
    seed_user(uid)
    body, status = _import(uid, _sealed_body(b"x" * 64))   # 64 bytes > 16 limit
    assert status == 413
    assert body["error"] == "material_too_large"
    assert body["max_bytes"] == 16


def test_resident_upload_is_idempotent(monkeypatch):
    monkeypatch.delenv("FEEDLING_RESIDENT_DISTILL_MAX_BYTES", raising=False)
    uid = "usr_res_up_idem"
    seed_user(uid)
    payload = _sealed_body(b"same-material", client_job_id="cj-idem")
    b1, s1 = _import(uid, payload)
    b2, s2 = _import(uid, payload)
    assert s1 == 200 and s2 == 200
    assert b1["job"]["job_id"] == b2["job"]["job_id"]     # same material → same job
    assert len(db.genesis_list_chunks(uid, b1["job"]["job_id"])) == 1   # not double-stored
