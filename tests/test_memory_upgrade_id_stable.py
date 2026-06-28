"""Regression: legacy→v1 memory.upgrade must keep the ORIGINAL memory_id even
when the caller's envelope carries a different id.

Hit in migration e2e (red line): the prebuilt-envelope upgrade path passes the
consumer's re-sealed envelope, which carries a fresh id. `_memory_record_from_envelope`
prefers `envelope["id"]`, so the upgraded card's embedded id changed — breaking the
"in-place upgrade, id stable" invariant (Garden/recall would treat it as a new card).
Pin it: the stored record's id must equal the target memory_id regardless of the
envelope's id, for BOTH the storage slot key and the embedded record id.
"""
from __future__ import annotations

import sys
import threading
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from memory import actions as memory_actions  # noqa: E402


def test_upgrade_keeps_memory_id_when_envelope_carries_new_id(monkeypatch):
    memory_id = "mom_original_123"
    existing = {
        "id": memory_id,
        "owner_user_id": "usr_test",
        "occurred_at": "2026-01-01T00:00:00Z",
        "created_at": "2026-01-01T00:00:00Z",
        "source": "live_conversation",
        "body_ct": "old_legacy_ciphertext",
        "nonce": "n0",
        "K_user": "ku0",
        "visibility": "shared",
        "type": "moment",
    }
    captured: dict = {}

    def fake_upsert(user_id, slot_id, occurred_at, doc):
        captured["user_id"] = user_id
        captured["slot_id"] = slot_id
        captured["doc"] = dict(doc)
        return True

    monkeypatch.setattr(memory_actions.memory_service, "_load_moments", lambda _s: [dict(existing)])
    monkeypatch.setattr(memory_actions.db, "memory_upsert", fake_upsert)
    monkeypatch.setattr(memory_actions.memory_service, "_append_memory_change", lambda _s, c: {"id": "chg", **c})

    store = types.SimpleNamespace(user_id="usr_test", memory_lock=threading.Lock())
    action = {
        "id": memory_id,
        "old_body_hash": "",  # skip CAS for this unit test
        "envelope": {
            "id": "mom_BRAND_NEW_should_be_ignored",
            "body_ct": "new_v1_ciphertext",
            "nonce": "n1",
            "K_user": "ku1",
            "K_enclave": "ke1",
            "visibility": "shared",
            "owner_user_id": "usr_test",
            "occurred_at": "2026-06-01T00:00:00Z",
        },
    }

    result, effects, status = memory_actions._memory_upgrade_envelope_action(store, action)

    assert status == 200, result
    assert result.get("status") == "ok", result
    # storage slot keyed by the original id …
    assert captured["slot_id"] == memory_id
    # … AND the embedded record id is pinned to the original, NOT the envelope's new id
    assert captured["doc"]["id"] == memory_id
    # content really did upgrade (new ciphertext written)
    assert captured["doc"]["body_ct"] == "new_v1_ciphertext"
