"""P2 request-layer: resident consumer endpoints core (pending/complete/heartbeat).

Integration against real Postgres: upload -> pending (claim + return sealed) -> complete
(done + delete material) / heartbeat (owner-only). Routes are thin wrappers over these.
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


def _ns(uid):
    return types.SimpleNamespace(user_id=uid)


def _sealed_body(ct: bytes, *, client_job_id="cj", mode="add_memory"):
    return {
        "format": "sealed_v1", "client_job_id": client_job_id, "mode": mode,
        "ciphertext_b64": base64.b64encode(ct).decode("ascii"),
        "ciphertext_sha256": hashlib.sha256(ct).hexdigest(),
        "content_sha256": hashlib.sha256(ct).hexdigest(),
        "aad": {"owner_user_id": "u", "v": 1, "item_id": "it"},
    }


def _upload(uid, ct=b"material", **kw):
    seed_user(uid)
    body, st = genesis_core._resident_sealed_import(_ns(uid), _sealed_body(ct, **kw))
    assert st == 200
    return body["job"]["job_id"]


def test_pending_claims_and_returns_sealed(monkeypatch):
    monkeypatch.delenv("FEEDLING_RESIDENT_DISTILL_MAX_BYTES", raising=False)
    uid = "usr_ep_pending"
    jid = _upload(uid, b"hello-sealed-material")
    body, st = genesis_core.resident_pending(_ns(uid), consumer_id="cons-A")
    assert st == 200
    mine = [j for j in body["jobs"] if j["job_id"] == jid]
    assert len(mine) == 1
    assert mine[0]["mode"] == "add_memory"
    assert mine[0]["sealed"]["ciphertext_b64"] == base64.b64encode(b"hello-sealed-material").decode()
    assert db.genesis_get_job(uid, jid)["status"] == "processing"     # claimed


def test_pending_requires_consumer_id():
    uid = "usr_ep_noc"
    seed_user(uid)
    body, st = genesis_core.resident_pending(_ns(uid), consumer_id="")
    assert st == 400 and body["error"] == "consumer_id_required"


def test_complete_marks_done_and_deletes_material(monkeypatch):
    monkeypatch.delenv("FEEDLING_RESIDENT_DISTILL_MAX_BYTES", raising=False)
    uid = "usr_ep_complete"
    jid = _upload(uid)
    genesis_core.resident_pending(_ns(uid), consumer_id="cons-A")     # claim first
    body, st = genesis_core.resident_complete(_ns(uid), jid, {"memory_action_count": 5})
    assert st == 200 and body["job"]["status"] == "done" and body["job"]["memory_action_count"] == 5
    assert db.genesis_get_job(uid, jid)["status"] == "done"
    assert db.genesis_list_chunks(uid, jid) == []                     # sealed material deleted


def test_complete_job_not_found():
    uid = "usr_ep_nf"
    seed_user(uid)
    body, st = genesis_core.resident_complete(_ns(uid), "genesis_nope", {})
    assert st == 404 and body["error"] == "job_not_found"


def test_heartbeat_owner_only(monkeypatch):
    monkeypatch.delenv("FEEDLING_RESIDENT_DISTILL_MAX_BYTES", raising=False)
    uid = "usr_ep_hb"
    jid = _upload(uid)
    genesis_core.resident_pending(_ns(uid), consumer_id="cons-A")     # claim as cons-A
    b1, s1 = genesis_core.resident_heartbeat(_ns(uid), jid, consumer_id="cons-A")
    assert s1 == 200 and b1["ok"] is True
    b2, s2 = genesis_core.resident_heartbeat(_ns(uid), jid, consumer_id="cons-B")
    assert s2 == 409 and b2["error"] == "heartbeat_rejected"
