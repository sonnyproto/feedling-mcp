"""Days-scaled memory-count consistency signal (NOT a gate).

A long relationship should have a proportional number of memory cards.
`_memory_floor_for_days` (backend/memory/service.py) reuses the existing
tiered `_per_tab_floors_for_days` TOTAL column (v2 has no tabs) so the
Seven-calibrated values (2 / 5 / 12 / 30) never drift out of sync.

bootstrap_status_payload (backend/bootstrap/status_core.py) surfaces this as
informational fields — `memory_floor`, `memory_aspiration`, and
`memory_below_floor` — so the App/agent can see "claimed 80 days but only 2
cards" without anything being blocked. Memory is not a gate (2026-06); these
fields don't change that.
"""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from bootstrap import status_core  # noqa: E402
from identity import service as identity_service  # noqa: E402
from memory import service as memory_service  # noqa: E402
from memory.service import (  # noqa: E402
    _memory_aspiration_for_days,
    _memory_floor_for_days,
)


def test_memory_floor_for_days_matches_tiered_reference_values():
    # Reference floors (TOTAL, by relationship days) — same tiers as
    # _per_tab_floors_for_days, reused rather than duplicated.
    assert _memory_floor_for_days(0) == 2
    assert _memory_floor_for_days(5) == 5
    assert _memory_floor_for_days(40) == 12
    assert _memory_floor_for_days(200) == 30


def test_memory_aspiration_for_days_matches_reference_values():
    assert _memory_aspiration_for_days(0) == 4
    assert _memory_aspiration_for_days(5) == 15
    assert _memory_aspiration_for_days(40) == 35
    assert _memory_aspiration_for_days(200) == 70


class _NoopLock:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Store:
    user_id = "usr_memory_floor_signal"
    chat_lock = _NoopLock()
    chat_messages: list[dict] = []


def _install_status_harness(monkeypatch, *, age_days: int, memories: list[dict], identity: dict | None):
    monkeypatch.setattr(identity_service, "_load_identity", lambda _store: identity)
    monkeypatch.setattr(identity_service, "_relationship_age_days", lambda _store: age_days)
    monkeypatch.setattr(memory_service, "_load_moments", lambda _store: memories)
    monkeypatch.setattr(status_core.boot_gates, "_chat_loop_verified_by_server", lambda _store: False)
    monkeypatch.setattr(
        status_core.chat_consumer,
        "_consumer_validation_state",
        lambda _store: {"passing": False},
    )


def test_status_flags_long_relationship_with_too_few_memories(monkeypatch):
    """~80 days of relationship with only 2 memory cards is well below the
    12-card floor for the >=30-day tier -> memory_below_floor must be True,
    surfaced informationally (no gate/block anywhere)."""
    store = _Store()
    _install_status_harness(
        monkeypatch,
        age_days=80,
        identity={
            "relationship_started_at": "2030-03-13",
            "relationship_anchor_source": "user_calibrated",
            "self_introduction": "hi",
            "updated_at": "2030-06-01T00:00:00",
        },
        memories=[
            {"id": "m1", "created_at": "2026-01-01T09:00:00", "tab": "story"},
            {"id": "m2", "created_at": "2026-01-02T09:00:00", "tab": "story"},
        ],
    )

    age_days = identity_service._relationship_age_days(store)
    assert age_days >= 30, f"test setup didn't land in the >=30-day tier: {age_days}"

    payload = status_core.bootstrap_status_payload(store)
    assert payload["memories_count"] == 2
    assert payload["memory_floor"] == 12
    assert payload["memory_aspiration"] == 35
    assert payload["memory_below_floor"] is True

    # Signal only — must not surface as / feed into a gate.
    assert "is_complete" in payload
    assert payload["is_complete"] in (True, False)


def test_status_fresh_account_below_trivial_floor_is_expected(monkeypatch):
    """A brand-new account (no identity, no memories) sits below the trivial
    2-card floor for the <2-day tier. That's expected and fine — the field
    is a signal, not something the client should treat as an error state."""
    store = _Store()
    _install_status_harness(monkeypatch, age_days=0, identity=None, memories=[])

    payload = status_core.bootstrap_status_payload(store)
    assert payload["memories_count"] == 0
    assert payload["memory_floor"] == 2
    assert payload["memory_aspiration"] == 4
    assert payload["memory_below_floor"] is True
