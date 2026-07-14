"""In-process TEE auto-sync scheduler (admin.tee_sync_scheduler).

Drives the same tee_replication.run_action a manual run would; these tests stub
run_action and assert the per-tick call sequence + skip semantics. (A completed
tick now also records one tee_sync_runs metrics row + probes TEE health — a
best-effort side effect covered by test_tee_sync_metrics.py; it is harmless
here and not asserted on.)
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from admin import tee_sync_scheduler as sched  # noqa: E402
from admin import tee_replication as tr  # noqa: E402


@pytest.fixture
def calls(monkeypatch):
    recorded = []

    def fake_run_action(*, action, table=None, dry_run=True, confirm=None, **kw):
        recorded.append((action, table, dry_run, confirm))
        return {"ok": True, "copied": 0, "pending": 0, "errors": 0}

    monkeypatch.setattr(tr, "run_action", fake_run_action)
    return recorded


def test_replicate_tick_hits_every_ciphertext_table(calls):
    sched._sync_tick(do_reconcile=False)
    replicated = [c for c in calls if c[0] == "replicate"]
    assert [c[1] for c in replicated] == list(sched._CIPHERTEXT_TABLES)
    # non-dry-run + confirm gate carried on every replicate
    assert all(c[2] is False and c[3] == "MIGRATE" for c in replicated)
    # do_reconcile=False → no reconcile/verify
    assert not any(c[0] in ("reconcile", "verify") for c in calls)


def test_reconcile_runs_before_replicate_then_verify(calls):
    # Order is load-bearing: reconcile backfills the plaintext `users` parent
    # BEFORE any ciphertext child replicate, or the children FK-fail.
    sched._sync_tick(do_reconcile=True)
    actions = [c[0] for c in calls]
    assert actions == ["reconcile"] + ["replicate"] * 5 + ["verify"]
    reconcile = next(c for c in calls if c[0] == "reconcile")
    verify = next(c for c in calls if c[0] == "verify")
    assert reconcile[3] == "MIGRATE"
    assert verify[2] is False  # dry_run=False, verify is confirm-exempt


def test_reconcile_failure_does_not_block_replicate(monkeypatch):
    # A reconcile error must be swallowed and replicate still attempted (the
    # loop degrades, never dies).
    calls = []

    def fake(*, action, table=None, dry_run=True, confirm=None, **kw):
        calls.append(action)
        if action == "reconcile":
            raise RuntimeError("reconcile boom")
        return {"ok": True}

    monkeypatch.setattr(tr, "run_action", fake)
    sched._sync_tick(do_reconcile=True)
    assert calls[0] == "reconcile"
    assert calls.count("replicate") == 5


def test_already_running_aborts_replicate_phase(monkeypatch):
    calls = []

    def fake(*, action, table=None, **kw):
        calls.append((action, table))
        if action == "reconcile":
            return {"tables": []}
        raise tr.AlreadyRunning()

    monkeypatch.setattr(tr, "run_action", fake)
    sched._sync_tick(do_reconcile=True)
    # reconcile ran; first replicate raises AlreadyRunning → return before the rest
    assert calls == [("reconcile", None), ("replicate", "chat_messages")]


def test_unconfigured_aborts_silently(monkeypatch):
    calls = []

    def fake(*, action, table=None, **kw):
        calls.append(action)
        if action == "reconcile":
            return {"tables": []}
        raise tr.Unconfigured()

    monkeypatch.setattr(tr, "run_action", fake)
    sched._sync_tick(do_reconcile=True)
    assert calls == ["reconcile", "replicate"]  # stopped on first replicate Unconfigured


def test_one_table_error_does_not_stop_the_pass(monkeypatch):
    seen = []

    def fake(*, action, table=None, dry_run=True, confirm=None, **kw):
        seen.append(table if action == "replicate" else action)
        if table == "memory_moments":
            raise RuntimeError("enclave hiccup")
        return {"ok": True}

    monkeypatch.setattr(tr, "run_action", fake)
    sched._sync_tick(do_reconcile=False)
    # memory_moments raised a generic error but the loop continued past it
    assert seen == list(sched._CIPHERTEXT_TABLES)


def test_start_spawns_a_daemon_thread(monkeypatch):
    started = {}

    class FakeThread:
        def __init__(self, target, daemon, name):
            started["daemon"] = daemon
            started["name"] = name

        def start(self):
            started["started"] = True

    monkeypatch.setattr(sched.threading, "Thread", FakeThread)
    sched.start()
    assert started == {"daemon": True, "name": "tee-sync", "started": True}


def test_sync_tick_returns_reconcile_success(calls):
    # Reconcile succeeds → True (caller may hold off next reconcile).
    assert sched._sync_tick(do_reconcile=True) is True
    # No reconcile due → True (no retry pressure).
    assert sched._sync_tick(do_reconcile=False) is True


def test_failed_reconcile_returns_false_for_soon_retry(monkeypatch):
    def fake(*, action, table=None, **kw):
        if action == "reconcile":
            raise RuntimeError("SSL eof")
        return {"ok": True}

    monkeypatch.setattr(tr, "run_action", fake)
    # reconcile failed → False so _loop won't advance last_reconcile (retries next tick).
    assert sched._sync_tick(do_reconcile=True) is False


def test_first_tick_always_reconciles_regardless_of_monotonic(monkeypatch):
    """首个 tick(last_reconcile is None)必 reconcile —— 建立 users 基线,不能靠
    monotonic() 的绝对值(宿主 uptime 小的新 CVM 上它 < reconcile 间隔 → 旧逻辑首 tick
    不 reconcile → FK 全线失败,2026-07-14 prod 实测)。"""
    monkeypatch.setenv("FEEDLING_TEE_RECONCILE_INTERVAL_SEC", "86400")
    # 新进程:last_reconcile=None,monotonic 才几秒(远 < 86400)——旧逻辑会返回 False。
    assert sched._should_reconcile(None, 5.0) is True
    # 已 reconcile 过:未到间隔不重跑
    assert sched._should_reconcile(1000.0, 1000.0 + 86399) is False
    # 到间隔:重跑
    assert sched._should_reconcile(1000.0, 1000.0 + 86400) is True
