"""Legacy memory card (pre-v1) → v1 migration — pure helpers (plan §3).

Detection + batch selection + per-user migration state, all side-effect free so
they unit-test without DB/enclave. The orchestration (decrypt batch → call_agent
derive v1 → memory.upgrade) lives in the resident consumer's migrate handler;
the trigger lives in capture_scheduler. Both build on these.

DECRYPT NOTE: judge shape on the RAW inner from the memory_action decrypt path
(`_memory_plain_from_envelope`). Do NOT judge on the readside output — the enclave
readside already adapts old→v1 for display (`enclave_app._memory_inner_to_v1`), so
a legacy card read there looks v1 and would never be detected.
"""
from __future__ import annotations

import hashlib
from typing import Any, Mapping

MIGRATION_STATE_BLOB = "memory_migration_state"
DEFAULT_MIGRATE_BATCH = 8

# v1 inners always carry {bucket, threads} (memory._memory_inner_from_action);
# old inners carry these content fields and none of the v1 structure.
_OLD_CONTENT_FIELDS = ("title", "description", "her_quote", "context", "linked_dimension")


def is_legacy_card_inner(inner: Mapping[str, Any] | None) -> bool:
    """True if a DECRYPTED RAW inner is a pre-v1 (old-schema) card."""
    if not isinstance(inner, Mapping):
        return False
    if ("bucket" in inner) and ("threads" in inner):
        return False  # has v1 structure
    return any(inner.get(k) for k in _OLD_CONTENT_FIELDS)


def body_hash(moment: Mapping[str, Any] | None) -> str:
    """CAS token = sha256 of the stored ciphertext (same token memory.upgrade
    checks). `to_v1_card` never touches body_ct, so it's stable across reads."""
    return hashlib.sha256(str((moment or {}).get("body_ct") or "").encode("utf-8")).hexdigest()


def select_legacy_batch(
    decrypted: list[tuple[Mapping[str, Any], Mapping[str, Any]]],
    *,
    batch_size: int = DEFAULT_MIGRATE_BATCH,
) -> list[dict]:
    """From [(moment, raw_inner), ...] pick up to batch_size legacy cards, each as
    {id, inner, old_body_hash} for the migrate→upgrade loop. Skips id-less rows."""
    out: list[dict] = []
    cap = max(1, int(batch_size))
    for moment, inner in decrypted:
        if not isinstance(moment, Mapping):
            continue
        mid = str(moment.get("id") or "")
        if not mid or not is_legacy_card_inner(inner):
            continue
        out.append({"id": mid, "inner": dict(inner), "old_body_hash": body_hash(moment)})
        if len(out) >= cap:
            break
    return out


def count_legacy(decrypted: list[tuple[Mapping[str, Any], Mapping[str, Any]]]) -> int:
    return sum(1 for _m, inner in decrypted if is_legacy_card_inner(inner))


def migrate_key_for_window(user_id: str, window_id: str) -> str:
    """Idempotent migrate-job key: one batch per user per quiet window."""
    return f"migrate:v1:{user_id}:{window_id}"[:240]


# --- per-user migration state blob (status: unknown | pending | done) ---

def initial_state() -> dict:
    return {"v": 1, "status": "unknown", "legacy_remaining": -1, "migrated_total": 0}


def migration_done(state: Mapping[str, Any] | None) -> bool:
    return isinstance(state, Mapping) and str(state.get("status") or "").lower() == "done"


def should_enqueue(state: Mapping[str, Any] | None) -> bool:
    """Enqueue a batch unless we've proven nothing is left (status==done). 'unknown'
    (never scanned) and 'pending' (more remain) both enqueue."""
    return not migration_done(state)


def next_state(
    state: Mapping[str, Any] | None,
    *,
    migrated: int,
    legacy_remaining: int,
) -> dict:
    """Compute the updated state after a batch. legacy_remaining<=0 ⇒ done."""
    base = dict(state) if isinstance(state, Mapping) else initial_state()
    base["v"] = 1
    base["migrated_total"] = int(base.get("migrated_total") or 0) + max(0, int(migrated))
    base["legacy_remaining"] = int(legacy_remaining)
    base["status"] = "done" if int(legacy_remaining) <= 0 else "pending"
    return base
