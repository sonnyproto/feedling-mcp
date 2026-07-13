"""Days-scaled memory-count consistency signal (NOT a gate).

A long relationship should have a proportional number of memory cards.
`_memory_floor_for_days` (backend/memory/service.py) reuses the existing
tiered `_per_tab_floors_for_days` TOTAL column (v2 has no tabs) so the
onboarding-gate era values (2 / 13 / 38 / 87) never drift out of sync.

bootstrap_status_payload (backend/bootstrap/status_core.py) surfaces this as
two informational fields — `memory_floor` and `memory_below_floor` — so the
App/agent can see "claimed 80 days but only 2 cards" without anything being
blocked. Memory is not a gate (2026-06); these fields don't change that.
"""

from __future__ import annotations

import base64
import itertools
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from bootstrap.status_core import bootstrap_status_payload  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from identity import service as identity_service  # noqa: E402
from memory.service import _memory_floor_for_days  # noqa: E402


def test_memory_floor_for_days_matches_tiered_reference_values():
    # Reference floors (TOTAL, by relationship days) — same tiers as
    # _per_tab_floors_for_days, reused rather than duplicated.
    assert _memory_floor_for_days(0) == 2
    assert _memory_floor_for_days(5) == 13
    assert _memory_floor_for_days(40) == 38
    assert _memory_floor_for_days(200) == 87


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    with make_client() as c:
        yield c


_pk_counter = itertools.count(1)


def _register(client) -> tuple[str, str]:
    raw = next(_pk_counter).to_bytes(32, "big")
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(raw), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def test_status_flags_long_relationship_with_too_few_memories(client):
    """~80 days of relationship with only 2 memory cards is well below the
    38-card floor for the >=30-day tier -> memory_below_floor must be True,
    surfaced informationally (no gate/block anywhere)."""
    user_id, api_key = _register(client)
    store = core_store.get_store(user_id)

    anchor = (datetime.now() - timedelta(days=80)).strftime("%Y-%m-%d")
    identity_service._save_identity(
        store,
        {
            "relationship_started_at": anchor,
            "relationship_anchor_source": "user_calibrated",
            "self_introduction": "hi",
            "updated_at": datetime.now().isoformat(),
        },
    )
    db.memory_replace_all(
        user_id,
        [
            {"id": "m1", "occurred_at": "2026-01-01T09:00:00", "tab": "story"},
            {"id": "m2", "occurred_at": "2026-01-02T09:00:00", "tab": "story"},
        ],
    )

    age_days = identity_service._relationship_age_days(store)
    assert age_days >= 30, f"test setup didn't land in the >=30-day tier: {age_days}"

    payload = bootstrap_status_payload(store)
    assert payload["memories_count"] == 2
    assert payload["memory_floor"] == 38
    assert payload["memory_below_floor"] is True

    # Signal only — must not surface as / feed into a gate.
    assert "is_complete" in payload
    assert payload["is_complete"] in (True, False)


def test_status_fresh_account_below_trivial_floor_is_expected(client):
    """A brand-new account (no identity, no memories) sits below the trivial
    2-card floor for the <2-day tier. That's expected and fine — the field
    is a signal, not something the client should treat as an error state."""
    user_id, api_key = _register(client)
    store = core_store.get_store(user_id)

    payload = bootstrap_status_payload(store)
    assert payload["memories_count"] == 0
    assert payload["memory_floor"] == 2
    assert payload["memory_below_floor"] is True
