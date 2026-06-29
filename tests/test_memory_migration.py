"""Unit tests for the legacy→v1 migration substrate (plan §3).

Covers the pure pieces (detection / state / prompt parse) + the enqueue guard.
The memory.upgrade in-place/CAS path is exercised by the memory-action tests.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from memory import migration  # noqa: E402
from memory import migrate_prompt_v1 as mp  # noqa: E402
from proactive import capture_jobs  # noqa: E402
from proactive import capture_scheduler  # noqa: E402


@pytest.fixture(autouse=True)
def _migration_on(monkeypatch):
    """A10 made migration default-OFF (FEEDLING_MIGRATE_ENABLE kill switch). Every
    test here predates that and assumes migration runs, so flip it ON for the suite;
    the dedicated gate tests below monkeypatch it back OFF where they need to."""
    monkeypatch.setenv("FEEDLING_MIGRATE_ENABLE", "1")


# --- is_legacy_card_inner -------------------------------------------------

def test_old_inner_is_legacy():
    old = {"title": "去西湖", "description": "上周一起去了西湖", "her_quote": "好美"}
    assert migration.is_legacy_card_inner(old) is True


def test_full_v1_inner_is_not_legacy():
    v1 = {"summary": "去西湖", "content": "上周一起去了西湖", "bucket": "出行", "threads": ["约会"]}
    assert migration.is_legacy_card_inner(v1) is False


def test_mixed_inner_still_legacy():
    # patched to carry bucket/threads but body is still old → must NOT be skipped
    mixed = {"bucket": "出行", "threads": ["约会"], "title": "去西湖", "description": "..."}
    assert migration.is_legacy_card_inner(mixed) is True


def test_non_dict_and_empty_not_legacy():
    assert migration.is_legacy_card_inner(None) is False
    assert migration.is_legacy_card_inner({}) is False  # no v1, but no old content either


# --- batch selection ------------------------------------------------------

def test_select_legacy_batch_filters_and_caps():
    moments = [
        ({"id": "m1", "body_ct": "ct1"}, {"title": "a", "description": "x"}),       # legacy
        ({"id": "m2", "body_ct": "ct2"}, {"summary": "s", "content": "c", "bucket": "b", "threads": []}),  # v1
        ({"id": "m3", "body_ct": "ct3"}, {"description": "d"}),                       # legacy
        ({"id": "", "body_ct": "ct4"}, {"title": "noid"}),                            # legacy but no id → skip
    ]
    batch = migration.select_legacy_batch(moments, batch_size=8)
    assert [b["id"] for b in batch] == ["m1", "m3"]
    assert all("old_body_hash" in b and b["old_body_hash"] for b in batch)
    # cap respected
    assert len(migration.select_legacy_batch(moments, batch_size=1)) == 1
    assert migration.count_legacy(moments) == 3


# --- migration state machine ---------------------------------------------

def test_should_enqueue_state_vs_observed():
    done = {"status": "done"}
    # cached state says done, but a real scan found a legacy card → re-enqueue (self-heal)
    assert migration.should_enqueue(done, observed_legacy_count=1) is True
    assert migration.should_enqueue(done, observed_legacy_count=0) is False
    # no observation → fall back to cached state
    assert migration.should_enqueue(done) is False
    assert migration.should_enqueue({"status": "unknown"}) is True
    assert migration.should_enqueue(None) is True


def test_next_state_done_vs_pending():
    s0 = migration.initial_state()
    s1 = migration.next_state(s0, migrated=3, legacy_remaining=5)
    assert s1["status"] == "pending" and s1["migrated_total"] == 3
    s2 = migration.next_state(s1, migrated=5, legacy_remaining=0)
    assert s2["status"] == "done" and s2["migrated_total"] == 8


def test_reaudit_due_only_when_done_and_aged():
    done = migration.next_state({"status": "done"}, migrated=0, legacy_remaining=0, now=1000.0)
    assert done["updated_at"] == 1000.0
    assert migration.reaudit_due(done, now=1000.0, reaudit_sec=100) is False      # fresh
    assert migration.reaudit_due(done, now=1000.0 + 200, reaudit_sec=100) is True  # aged → re-scan
    pending = {"status": "pending", "updated_at": 0.0}
    assert migration.reaudit_due(pending, now=1e9, reaudit_sec=100) is False       # not done → n/a


# --- prompt parse ---------------------------------------------------------

def test_parse_drops_bad_dup_empty_and_outofbatch():
    allowed = {"m1", "m2", "m3"}
    raw = """```json
    {"upgrades": [
      {"id": "m1", "summary": "s1", "content": "c1", "bucket": "b", "threads": ["t"]},
      {"id": "m1", "summary": "dup", "content": "c"},
      {"id": "mX", "summary": "out of batch", "content": "c"},
      {"id": "m2", "summary": "", "content": ""},
      {"id": "m3", "summary": "s3", "content": "c3"}
    ]}
    ```"""
    upgrades, unmigrated, error = mp.parse_migrated_cards(raw, allowed_ids=allowed)
    assert error is None
    assert [u["id"] for u in upgrades] == ["m1", "m3"]
    # m2 (empty) + the never-valid mX is not in batch; unmigrated = batch ids not upgraded
    assert set(unmigrated) == {"m2"}


def test_parse_no_json_marks_all_unmigrated():
    upgrades, unmigrated, error = mp.parse_migrated_cards("no json here", allowed_ids={"m1", "m2"})
    assert upgrades == [] and error == "no_json_object"
    assert set(unmigrated) == {"m1", "m2"}


# --- enqueue guard (single-flight across maintenance) ---------------------

class _FakeStore:
    def __init__(self, jobs=None):
        self.user_id = "usr_test"
        self._jobs = list(jobs or [])

    def list_proactive_jobs(self, since_epoch=0, limit=0):
        return list(self._jobs)

    def append_proactive_job(self, job):
        self._jobs.append(job)
        return job


def test_enqueue_migrate_blocked_by_active_capture():
    store = _FakeStore([{"job_kind": "memory_capture", "status": "pending"}])
    job, enqueued, reason = capture_jobs.enqueue_memory_migrate_job(
        store, trigger="quiet", migrate_key="migrate:v1:u:w1")
    assert enqueued is False and reason == "maintenance_already_pending"


def test_enqueue_migrate_blocked_by_active_dream():
    store = _FakeStore([{"job_kind": "memory_dream", "status": "claimed"}])
    _job, enqueued, reason = capture_jobs.enqueue_memory_migrate_job(
        store, trigger="quiet", migrate_key="migrate:v1:u:w1")
    assert enqueued is False and reason == "maintenance_already_pending"


def test_enqueue_migrate_clean_then_dup():
    store = _FakeStore()
    job, enqueued, reason = capture_jobs.enqueue_memory_migrate_job(
        store, trigger="quiet", migrate_key="migrate:v1:u:w1")
    assert enqueued is True and reason == "enqueued"
    assert capture_jobs.is_memory_migrate_job(job) and capture_jobs.is_memory_maintenance_job(job)
    # same key again → idempotent, not re-enqueued
    _job2, enqueued2, reason2 = capture_jobs.enqueue_memory_migrate_job(
        store, trigger="quiet", migrate_key="migrate:v1:u:w1")
    assert enqueued2 is False and reason2 in ("duplicate_migrate_key", "maintenance_already_pending")


# --- A10 kill switch: FEEDLING_MIGRATE_ENABLE -----------------------------

def test_migration_enabled_flag_parsing(monkeypatch):
    for val in ("1", "true", "TRUE", "yes", "on", " On "):
        monkeypatch.setenv("FEEDLING_MIGRATE_ENABLE", val)
        assert migration.migration_enabled() is True, val
    for val in ("", "0", "false", "no", "off", "nope", "2"):
        monkeypatch.setenv("FEEDLING_MIGRATE_ENABLE", val)
        assert migration.migration_enabled() is False, val
    monkeypatch.delenv("FEEDLING_MIGRATE_ENABLE", raising=False)
    assert migration.migration_enabled() is False  # unset → off


def test_enqueue_migrate_disabled_short_circuits(monkeypatch):
    monkeypatch.setenv("FEEDLING_MIGRATE_ENABLE", "0")
    store = _FakeStore()
    job, enqueued, reason = capture_jobs.enqueue_memory_migrate_job(
        store, trigger="quiet", migrate_key="migrate:v1:u:w1")
    assert job is None and enqueued is False and reason == "migration_disabled"
    assert store._jobs == []  # nothing enqueued while off


def test_tick_quiet_migrate_disabled_short_circuits(monkeypatch):
    # Gate returns before any state/db read, so a bare fake store is enough.
    monkeypatch.setenv("FEEDLING_MIGRATE_ENABLE", "off")
    result = capture_scheduler.tick_quiet_migrate(_FakeStore())
    assert result == {"enqueued": False, "reason": "migration_disabled", "job": None}
