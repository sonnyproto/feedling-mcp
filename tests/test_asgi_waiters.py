"""Async waiter registry unit tests (ASGI-migration plan §9 / §14.2).

Pure-unit (no DB, no app): register/unregister/wake semantics, the global +
per-user caps that bound the failure domain, cross-thread wake (the LISTEN
thread calls wake), and wake_all (shutdown).
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from runtime.waiters import WaiterRegistry  # noqa: E402


def test_register_wake_unregister():
    async def go():
        reg = WaiterRegistry(max_active=100)
        w = reg.register("chat", "u1", per_user_max=2)
        assert w is not None
        assert reg.active_count() == 1
        reg.wake("chat", "u1")
        await asyncio.wait_for(w.event.wait(), timeout=1.0)  # woken
        reg.unregister(w)
        assert reg.active_count() == 0

    asyncio.run(go())


def test_unregister_is_idempotent():
    async def go():
        reg = WaiterRegistry(max_active=100)
        w = reg.register("chat", "u1", per_user_max=2)
        reg.unregister(w)
        reg.unregister(w)  # no double-decrement / crash
        assert reg.active_count() == 0

    asyncio.run(go())


def test_per_user_cap_sheds_third():
    async def go():
        reg = WaiterRegistry(max_active=100)
        a = reg.register("chat", "u1", per_user_max=2)
        b = reg.register("chat", "u1", per_user_max=2)
        c = reg.register("chat", "u1", per_user_max=2)
        assert a is not None and b is not None
        assert c is None  # 3rd over the per-user cap → shed
        assert reg.active_count() == 2
        # a different user is unaffected
        d = reg.register("chat", "u2", per_user_max=2)
        assert d is not None

    asyncio.run(go())


def test_global_cap_sheds():
    async def go():
        reg = WaiterRegistry(max_active=1)
        a = reg.register("chat", "u1", per_user_max=5)
        b = reg.register("chat", "u2", per_user_max=5)
        assert a is not None
        assert b is None  # global cap hit even though different user

    asyncio.run(go())


def test_channels_are_independent():
    async def go():
        reg = WaiterRegistry(max_active=100)
        c = reg.register("chat", "u1", per_user_max=1)
        p = reg.register("proactive", "u1", per_user_max=1)
        assert c is not None and p is not None  # same user, different channel
        reg.wake("chat", "u1")
        await asyncio.wait_for(c.event.wait(), timeout=1.0)
        assert not p.event.is_set()  # proactive waiter not woken by chat wake

    asyncio.run(go())


def test_wake_from_other_thread():
    async def go():
        reg = WaiterRegistry(max_active=10)
        w = reg.register("chat", "u1", per_user_max=2)
        # The wake-bus LISTEN thread calls wake() from OUTSIDE the loop.
        threading.Thread(target=lambda: reg.wake("chat", "u1")).start()
        await asyncio.wait_for(w.event.wait(), timeout=1.0)

    asyncio.run(go())


def test_wake_all():
    async def go():
        reg = WaiterRegistry(max_active=10)
        ws = [reg.register("chat", f"u{i}", per_user_max=2) for i in range(3)]
        reg.wake_all()
        for w in ws:
            await asyncio.wait_for(w.event.wait(), timeout=1.0)

    asyncio.run(go())
