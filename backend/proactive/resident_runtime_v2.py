"""Resident Runtime V2 rollout helpers.

This is an operations cutover flag, not a user-facing switch. Resident users do
not have the hosted model_api runtime profile, so the flag lives in its own
per-user blob and defaults off.
"""
from __future__ import annotations

from typing import Any, Mapping

import db
from core.store import UserStore

RESIDENT_RUNTIME_PROFILE_KIND_V2 = "resident_runtime_v2"
RESIDENT_WAKE_RUNTIME_V2_FLAG = "resident_wake_runtime_v2_enabled"


def load_resident_runtime_profile_v2(store: UserStore) -> dict[str, Any]:
    doc = db.get_blob(store.user_id, RESIDENT_RUNTIME_PROFILE_KIND_V2)
    return dict(doc) if isinstance(doc, Mapping) else {}


def resident_wake_runtime_v2_enabled(store: UserStore) -> bool:
    try:
        return bool(load_resident_runtime_profile_v2(store).get(RESIDENT_WAKE_RUNTIME_V2_FLAG))
    except Exception:
        return False


def resident_runtime_v2_public_profile(store: UserStore) -> dict[str, Any]:
    return {
        RESIDENT_WAKE_RUNTIME_V2_FLAG: resident_wake_runtime_v2_enabled(store),
    }
