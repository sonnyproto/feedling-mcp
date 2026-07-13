"""Custom gunicorn worker class for the ASGI backend (plan §5.2).

``limit_concurrency`` is a uvicorn setting with no gunicorn CLI flag, so it can
only be passed via a worker subclass' ``CONFIG_KWARGS``. It is a last-resort
guard set well above the poll-waiter cap (``FEEDLING_POLLER_MAX_ACTIVE``) plus
normal concurrency, so ordinary load never trips it.

Sizing (uvicorn 503s once EITHER count crosses the limit):

- uvicorn counts **open connections**, not just in-flight requests
  (``len(self.connections) >= limit_concurrency`` in its http protocol). Since
  keep-alive connections now idle for 75s rather than 2s
  (``backend/gunicorn_conf.py``), each online app parks a few pooled sockets
  here — so idle sockets, which consume no thread and no DB connection, push
  toward a guard meant to fire only under real load.
- The old 2048 also sat BELOW the poll-waiter cap it claims to clear
  (``FEEDLING_POLLER_MAX_ACTIVE`` defaults to 5000, ``asgi/settings.py``), so
  the documented invariant did not actually hold.

8192 clears the poll-waiter cap with room for the pooled sockets on top. The
real backpressure is elsewhere and unchanged: the ``run_db`` threadpool and the
16-connection DB pool. File descriptors are not a constraint — the deploys raise
``nofile`` to 65536 (Docker's 1024 default is what gunicorn would otherwise get).

Start command uses ``-k asgi.worker.FeedlingUvicornWorker`` (resolvable under
``--chdir backend``). Note: uvicorn's own ``uvicorn.workers`` is deprecated since
0.30 — we subclass the standalone ``uvicorn_worker`` package instead.
"""

from __future__ import annotations

from uvicorn_worker import UvicornWorker


class FeedlingUvicornWorker(UvicornWorker):
    CONFIG_KWARGS = {"limit_concurrency": 8192}
