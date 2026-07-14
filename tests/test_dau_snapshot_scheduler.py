from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin import dau_snapshot_scheduler as sched  # noqa: E402
import asgi.lifespan as lifespan_mod  # noqa: E402
from core import leader as core_leader  # noqa: E402


def test_tick_delegates_to_beijing_snapshot_job(monkeypatch):
    calls = []

    def freeze(*, now_epoch=None, tz):
        calls.append((now_epoch, tz))
        return ["2030-06-03"]

    monkeypatch.setattr(sched.db, "freeze_completed_dau_days", freeze)
    assert sched._tick(now_epoch=123.0) == ["2030-06-03"]
    assert calls == [(123.0, "Asia/Shanghai")]


def test_start_spawns_daemon_thread(monkeypatch):
    started = {}

    class FakeThread:
        def __init__(self, *, target, daemon, name):
            started.update(target=target, daemon=daemon, name=name)

        def start(self):
            started["started"] = True

    monkeypatch.setattr(sched.threading, "Thread", FakeThread)
    sched.start()
    assert started == {
        "target": sched._loop,
        "daemon": True,
        "name": "dau-snapshot",
        "started": True,
    }


def test_lifespan_leader_uses_distinct_singleton_name(monkeypatch):
    calls = []
    monkeypatch.setattr(
        core_leader,
        "run_singleton",
        lambda name, start_fn: calls.append((name, start_fn)),
    )
    lifespan_mod._start_dau_snapshot_leader()
    assert calls == [("dau-snapshot", sched.start)]
