"""Regression: concurrent same-user memory writes must not lost-update.

Every memory mutation used to do a lock-FREE ``_load_moments`` → mutate →
``_save_moments`` (which only locked the final write). ``memory_replace_all``
reconciles the passed snapshot against the live DB and DELETEs any row absent
from that snapshot — so a save built on a stale read silently deletes a card a
concurrent same-user write added in between. Under Flask ``-w 1`` same-user
requests serialized and masked this; the ASGI threadpool makes them truly
overlap, exposing it as live data loss (HIGH).

The fix holds ``store.memory_lock`` (now an RLock) across load→mutate→save and
re-reads INSIDE the lock. This test drives the exact race deterministically: the
first (validation) load injects a concurrent card into the DB but returns the
pre-injection snapshot; only a save that re-reads inside the lock preserves it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from accounts import registry  # noqa: E402
from core import store as core_store  # noqa: E402
from memory import memory_core  # noqa: E402
from memory import service as memory_service  # noqa: E402


def _card(uid: str, mid: str, occurred_at: str) -> dict:
    return {
        "id": mid,
        "type": "fact",
        "occurred_at": occurred_at,
        "owner_user_id": uid,
        "body_ct": f"ct_{mid}",
        "nonce": f"n_{mid}",
        "K_user": f"ku_{mid}",
        "visibility": "local_only",
    }


def test_add_does_not_delete_a_concurrently_added_card(monkeypatch):
    registry.load_users()
    uid = registry._register_user()["user_id"]  # real users-table row (FK parent)
    store = core_store.get_store(uid)

    # Seed the pre-existing card A.
    db.memory_upsert(uid, "A", "2026-01-01", _card(uid, "A", "2026-01-01"))

    # racy_load: the first call (add's validation load) reads the DB, THEN a
    # concurrent same-user write lands (card C), but the call returns the
    # pre-C snapshot. A save that re-reads inside the lock (the fix) sees C; the
    # old single-read path would replace_all([A,B]) and delete C.
    real_load = memory_service._load_moments
    calls = {"n": 0}

    def racy_load(s):
        snap = real_load(s)
        calls["n"] += 1
        if calls["n"] == 1:
            db.memory_upsert(uid, "C", "2026-01-02", _card(uid, "C", "2026-01-02"))
        return snap

    monkeypatch.setattr(memory_service, "_load_moments", racy_load)

    body, status = memory_core.add(
        store,
        {"envelope": {**_card(uid, "B", "2026-01-03"), "type": "fact"}},
    )
    assert status == 201, body

    final_ids = {str(m.get("id")) for m in db.memory_load(uid)}
    assert final_ids == {"A", "B", "C"}, (
        f"lost-update: expected A,B,C but got {final_ids} "
        f"(C was deleted by a stale-snapshot save)"
    )
