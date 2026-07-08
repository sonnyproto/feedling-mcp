"""P3: identity.replace server-build action — high-risk gating (Codex P1).

Full-card overwrite must be usable ONLY inside a live resident-distill job context, never
as a normal agent action. These tests lock the gate; the replace semantics themselves are
covered by the genesis replace_identity_preserving_anchor tests.
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent))

import db  # noqa: E402
from identity import actions  # noqa: E402
from conftest import seed_user  # noqa: E402

_IDENTITY = {"agent_name": "Nyx", "self_introduction": "hi", "dimensions": [{"name": "warmth", "value": 60}]}


def _ns(uid):
    return types.SimpleNamespace(user_id=uid)


def _run(store, action):
    return actions._execute_identity_action(store, None, action, runtime_token="")


def _live_resident_job(uid, jid="job_idrep"):
    seed_user(uid)
    db.genesis_create_job(uid, {"job_id": jid, "status": "awaiting_resident"})
    db.genesis_claim_resident_jobs(uid, consumer_id="cons-A")   # -> processing, resident-owned
    return jid


def test_replace_rejected_without_distill_context():
    r, _e, st = _run(_ns("u1"), {"type": "identity.replace", "identity": _IDENTITY})
    assert st == 403 and r["error"] == "identity_replace_requires_resident_distill_context"


def test_replace_rejected_when_payload_carries_envelope():
    r, _e, st = _run(_ns("u2"), {"type": "identity.replace", "envelope": {"body_ct": "x"},
                                 "source": "genesis_resident_distill", "job_id": "j", "reason": "r",
                                 "identity": _IDENTITY})
    assert st == 400 and r["error"] == "envelope_not_allowed"


def test_replace_rejected_when_job_not_a_live_resident_job():
    uid = "usr_idrep_nojob"
    seed_user(uid)
    r, _e, st = _run(_ns(uid), {"type": "identity.replace", "source": "genesis_resident_distill",
                                "job_id": "does_not_exist", "reason": "r", "identity": _IDENTITY})
    assert st == 403 and r["error"] == "not_a_live_resident_distill_job"


def test_replace_valid_context_passes_gate_and_reaches_replace():
    # Valid gate + live resident job, but the user has no initialized identity yet →
    # the replace itself returns identity_not_initialized (409). That the call reaches this
    # error proves the gate passed and dispatched into replace_identity_preserving_anchor.
    uid = "usr_idrep_ok"
    jid = _live_resident_job(uid)
    r, _e, st = _run(_ns(uid), {"type": "identity.replace", "source": "genesis_resident_distill",
                                "job_id": jid, "reason": "redefine persona", "identity": _IDENTITY})
    assert st == 409 and r["error"] == "identity_not_initialized"


def test_replace_in_supported_list_on_unknown_action():
    r, _e, st = _run(_ns("u3"), {"type": "identity.bogus"})
    assert st == 400
    assert "identity.replace" in r["supported"]
