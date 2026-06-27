"""Unit tests for the legacy→v1 migration substrate (plan §3).

Covers the pure pieces (detection / state / prompt parse) + the enqueue guard.
The memory.upgrade in-place/CAS path is exercised by the memory-action tests.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from memory import migration  # noqa: E402
from memory import migrate_prompt_v1 as mp  # noqa: E402
from proactive import capture_jobs  # noqa: E402


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
