"""Backend-side regression used by the deterministic memory contract probe."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from memory import actions as memory_actions  # noqa: E402
from memory import migration as memory_migration  # noqa: E402


def _stored(memory_id: str, body_ct: str) -> dict:
    return {
        "id": memory_id,
        "owner_user_id": "usr_memory_contract",
        "occurred_at": "2026-07-14T00:00:00Z",
        "created_at": "2026-07-14T00:00:00Z",
        "source": "qa_memory_contract",
        "body_ct": body_ct,
        "nonce": "nonce",
        "K_user": "wrapped-user-key",
        "K_enclave": "wrapped-enclave-key",
        "visibility": "shared",
    }


def _upgrade_envelope(memory_id: str, body_ct: str) -> dict:
    return {
        "id": memory_id,
        "owner_user_id": "usr_memory_contract",
        "body_ct": body_ct,
        "nonce": "new-nonce",
        "K_user": "new-wrapped-user-key",
        "K_enclave": "new-wrapped-enclave-key",
        "visibility": "shared",
        "occurred_at": "2026-07-14T00:00:00Z",
    }


def test_memory_upgrade_stale_cas_preserves_winner_and_concurrent_card(monkeypatch):
    """A stale migration write cannot overwrite the winner or drop another card."""
    memory_id = "mom_qa_legacy"
    sentinel_id = "mom_qa_concurrent_sentinel"
    state = {
        memory_id: _stored(memory_id, "legacy-ciphertext"),
        sentinel_id: _stored(sentinel_id, "concurrent-ciphertext"),
    }
    store = SimpleNamespace(
        user_id="usr_memory_contract",
        memory_lock=threading.Lock(),
    )
    old_hash = memory_actions._memory_body_hash(state[memory_id])

    monkeypatch.setattr(memory_migration, "migration_enabled", lambda: True)
    monkeypatch.setattr(
        memory_actions.memory_service,
        "_load_moments",
        lambda _store: [dict(value) for value in state.values()],
    )

    def upsert(_user_id, slot_id, _occurred_at, document):
        state[slot_id] = dict(document)
        return True

    monkeypatch.setattr(memory_actions.db, "memory_upsert", upsert)
    monkeypatch.setattr(
        memory_actions.memory_service,
        "_append_memory_change",
        lambda _store, change: {"id": "chg_qa", **change},
    )

    winner, _effects, winner_status = memory_actions._memory_upgrade_apply(
        store,
        memory_id=memory_id,
        envelope=_upgrade_envelope(memory_id, "winner-ciphertext"),
        old_body_hash=old_hash,
    )
    stale, stale_effects, stale_status = memory_actions._memory_upgrade_apply(
        store,
        memory_id=memory_id,
        envelope=_upgrade_envelope(memory_id, "stale-ciphertext"),
        old_body_hash=old_hash,
    )

    assert winner_status == 200 and winner["status"] == "ok"
    assert winner["memory"]["id"] == memory_id
    assert stale_status == 200
    assert stale == {
        "status": "ok",
        "action": "memory.upgrade",
        "skipped": "stale",
        "noop": True,
    }
    assert stale_effects == []
    assert state[memory_id]["id"] == memory_id
    assert state[memory_id]["body_ct"] == "winner-ciphertext"
    assert state[sentinel_id]["body_ct"] == "concurrent-ciphertext"
