"""Custom gunicorn worker class for the ASGI backend (plan §5.2).

``limit_concurrency`` is a uvicorn setting with no gunicorn CLI flag, so it can
only be passed via a worker subclass' ``CONFIG_KWARGS``. It is a last-resort
guard set well above the poll-waiter cap (``FEEDLING_POLLER_MAX_ACTIVE``) plus
normal concurrency, so ordinary load never trips it.

Start command uses ``-k asgi.worker.FeedlingUvicornWorker`` (resolvable under
``--chdir backend``). Note: uvicorn's own ``uvicorn.workers`` is deprecated since
0.30 — we subclass the standalone ``uvicorn_worker`` package instead.
"""

from __future__ import annotations

from uvicorn_worker import UvicornWorker


class FeedlingUvicornWorker(UvicornWorker):
    CONFIG_KWARGS = {"limit_concurrency": 2048}
