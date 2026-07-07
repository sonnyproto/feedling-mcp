"""Single-runner election over Postgres advisory locks.

Some background work must run exactly once across the whole backend — the
WebSocket ingest server (binds a fixed port) and the hosted tick scheduler
(creates wake jobs). Under ``-w 1`` that was automatic. Under ``-w N`` we elect
one worker per job: it grabs a session-level ``pg_try_advisory_lock`` and keeps
that connection open for as long as it runs the job; the losers retry on an
interval. If the holder dies, Postgres drops the lock and a loser takes over.
No new infra — the same Postgres everything else uses.

Scope note: ``start_fn`` runs at most once per process (the started-guard), so a
brief lock loss/re-acquire on a live worker won't double-bind the WS port. The
rare window where a holder's lock connection drops while its job threads keep
running (so another worker also starts the job) is acceptable for the
single-CVM target and bounded by the keepalive interval; revisit if we go
multi-instance with a flaky DB link.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time

import db
from core import wake_bus  # for WORKER_ID in logs

log = logging.getLogger("feedling.leader")

_RETRY_SEC = 10.0       # how often a loser retries the lock
_KEEPALIVE_SEC = 15.0   # how often the holder pings to detect a dropped lock

_started_lock = threading.Lock()
_started: set[str] = set()


def _lock_key(name: str) -> int:
    """Stable 63-bit signed advisory-lock key from a name."""
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _start_once(name: str, start_fn) -> None:
    with _started_lock:
        if name in _started:
            return
        _started.add(name)
    start_fn()


def _elect_loop(name: str, start_fn) -> None:
    key = _lock_key(name)
    while True:
        conn = None
        try:
            conn = db.listen_connection()  # dedicated, pool-external, autocommit
            won = conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]
            if not won:
                conn.close()
                conn = None
                time.sleep(_RETRY_SEC)
                continue
            log.info("[leader] won '%s' (worker=%s); starting singleton", name, wake_bus.WORKER_ID)
            _start_once(name, start_fn)
            # Hold the lock by keeping this connection open. The keepalive ping
            # both keeps the session live and detects a dropped connection, at
            # which point Postgres has released the lock and we re-elect.
            while True:
                time.sleep(_KEEPALIVE_SEC)
                conn.execute("SELECT 1")
        except Exception as e:
            log.warning("[leader] '%s' election/hold error: %s; retrying in %ss", name, e, _RETRY_SEC)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            time.sleep(_RETRY_SEC)


def run_singleton(name: str, start_fn) -> None:
    """Run ``start_fn`` on exactly one worker. Spawns a daemon election thread;
    the winner calls ``start_fn`` once and holds the lock, losers wait and take
    over if the winner dies. Call from the asgi/lifespan.py assembly section."""
    threading.Thread(
        target=_elect_loop, args=(name, start_fn), daemon=True, name=f"leader-{name}"
    ).start()
