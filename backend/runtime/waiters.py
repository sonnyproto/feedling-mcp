"""Async long-poll waiter registry (ASGI-migration plan §9 / §5.1).

Replaces the Flask ``threading.Event`` waiters: a long-poll that finds nothing
pending parks an ``asyncio.Event`` here instead of blocking an OS thread, so
thousands of idle polls cost futures, not threads (the whole point — plan §9.5).

Wake sources (all funnel through ``wake()``, which is thread-safe):
- **same-worker write** → ``core.store.notify_*_waiters`` fires the injected hook
  directly (does NOT go through the self-origin-filtered wake bus, closing the
  §19.2 gap);
- **cross-worker write** → the wake-bus LISTEN thread dispatches
  ``_wake_store_waiters`` → same store hook.

Each waiter captures its own event loop at register time, so ``wake()`` can be
called from any thread (the LISTEN thread or the loop thread) and correctly
schedules ``event.set`` on the right loop.
"""

from __future__ import annotations

import asyncio
import threading
from collections import defaultdict
from typing import Optional


class Waiter:
    __slots__ = ("channel", "user_id", "loop", "event")

    def __init__(self, channel: str, user_id: str, loop: asyncio.AbstractEventLoop):
        self.channel = channel
        self.user_id = user_id
        self.loop = loop
        self.event = asyncio.Event()


class WaiterRegistry:
    def __init__(self, *, max_active: int):
        self._by_key: dict[tuple[str, str], set[Waiter]] = defaultdict(set)
        self._lock = threading.Lock()
        self._active = 0
        self._max_active = max_active

    def register(self, channel: str, user_id: str, *, per_user_max: int) -> Optional[Waiter]:
        """Park a new waiter, or None if a cap is hit (caller sheds to timeout).

        Must be called from the event-loop thread (captures the running loop).
        """
        loop = asyncio.get_running_loop()
        key = (channel, user_id)
        with self._lock:
            if self._active >= self._max_active:
                return None
            if len(self._by_key[key]) >= per_user_max:
                return None
            waiter = Waiter(channel, user_id, loop)
            self._by_key[key].add(waiter)
            self._active += 1
            return waiter

    def unregister(self, waiter: Waiter) -> None:
        """Idempotent removal — always call in a ``finally`` so a cancelled poll
        never leaks a waiter (plan §14.6)."""
        key = (waiter.channel, waiter.user_id)
        with self._lock:
            bucket = self._by_key.get(key)
            if bucket and waiter in bucket:
                bucket.discard(waiter)
                self._active -= 1
                if not bucket:
                    self._by_key.pop(key, None)

    def wake(self, channel: str, user_id: str) -> None:
        """Wake all waiters for (channel, user_id). Thread-safe: callable from
        the LISTEN thread or the loop thread. Spurious wakes are harmless — the
        waiter always re-checks pending after waking."""
        key = (channel, user_id)
        with self._lock:
            waiters = list(self._by_key.get(key, ()))
        for waiter in waiters:
            try:
                waiter.loop.call_soon_threadsafe(waiter.event.set)
            except RuntimeError:
                # loop already closed (shutdown) — nothing to wake.
                pass

    def wake_all(self) -> None:
        """Wake every parked waiter (shutdown: let polls return promptly)."""
        with self._lock:
            waiters = [w for bucket in self._by_key.values() for w in bucket]
        for waiter in waiters:
            try:
                waiter.loop.call_soon_threadsafe(waiter.event.set)
            except RuntimeError:
                pass

    def active_count(self) -> int:
        with self._lock:
            return self._active


# Process-global registry (one per worker), sized from settings. The lifespan
# injects `registry.wake` into core.store as the async wake hook.
from asgi.settings import settings  # noqa: E402  (after class def to avoid cycle noise)

registry = WaiterRegistry(max_active=settings.poller_max_active)
