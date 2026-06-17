"""Multi-worker pieces of the hosted wake path (Step 4 of the -w N work).

Two units make the per-worker, key-gated tick safe under -w N:
  * db.try_stamp_hosted_tick — an atomic per-user heartbeat-slot CAS, so two
    workers that both hold a user's key can't each create a heartbeat in the
    same interval.
  * wake_consumer._hosted_keyholder_user_ids — restricts a worker's tick to the
    users whose plaintext key it actually holds (creation + the model call both
    need the key, so they must run where the key lives).

Run:  python -m pytest tests/test_hosted_wake_distribution.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import app as appmod  # noqa: E402  (wires flask_app + hooks)
import db  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import wake_consumer  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_stores():
    core_store._stores.clear()
    yield
    core_store._stores.clear()


def test_try_stamp_hosted_tick_is_an_atomic_cas():
    uid = "usr_stamp_cas_test"
    db.delete_blob(uid, "hosted_tick")
    interval = 1800.0
    now = 100_000.0
    # First stamp wins.
    assert db.try_stamp_hosted_tick(uid, {"ts": now}, now, interval) is True
    # A second worker in the same interval loses (no double heartbeat).
    assert db.try_stamp_hosted_tick(uid, {"ts": now + 5}, now + 5, interval) is False
    # Past the interval, the next stamp wins again.
    later = now + interval + 1
    assert db.try_stamp_hosted_tick(uid, {"ts": later}, later, interval) is True
    db.delete_blob(uid, "hosted_tick")


def test_keyholder_ids_only_returns_workers_own_keyed_users():
    with_key = core_store.get_store("usr_has_key")
    with_key.last_seen_api_key = "plaintext-key"
    core_store.get_store("usr_no_key")  # cached but no key on this worker

    ids = wake_consumer._hosted_keyholder_user_ids()
    assert "usr_has_key" in ids
    assert "usr_no_key" not in ids


def test_cross_worker_consume_noop_without_key():
    # try_consume_pending_for_user must be a cheap no-op for a user this worker
    # doesn't hold the key for — and must not load an uncached store.
    wake_consumer.try_consume_pending_for_user("usr_never_seen")
    assert "usr_never_seen" not in core_store._stores
