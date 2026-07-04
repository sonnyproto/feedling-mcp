"""Framework-neutral request-arg context — a flask-free replacement for the
throwaway ``flask.request`` the admin / proactive debug-page bridges used to
build.

Those internal HTML pages read query args (and, for the dashboard, the
``Accept-Language`` header) deep inside the render/​payload helpers via the
``flask.request`` global. Post-ASGI-cutover we no longer want a flask
application context on the hot-blocking path, so the native routes bind a tiny
neutral context built from the ASGI request's raw query string + headers, and
the helpers read it through the module-level ``request`` proxy below.

Only the sliver of the flask.request API those pages actually use is
reproduced:

    request.args.get(key[, default])      # first value, default None (flask parity)
    request.args.to_dict(flat=True)        # {key: first-value}
    request.headers.get(key[, default])    # case-insensitive

The proxy is contextvar-backed (mirroring flask's request-local), so it is safe
under ``asgi.threadpool.run_db`` worker threads: each ``bind()`` sets the value
for the duration of the synchronous render call on that thread. When unbound the
proxy returns an empty context (``.get()`` -> ``None``) rather than raising, so
bare unit-test calls degrade to defaults instead of blowing up.
"""

from __future__ import annotations

import contextlib
import contextvars
from urllib.parse import parse_qsl


class _Args:
    """Read-only view over a parsed query string (first-value-wins)."""

    def __init__(self, query_string: str = "") -> None:
        self._first: dict[str, str] = {}
        for key, value in parse_qsl(query_string or "", keep_blank_values=True):
            self._first.setdefault(key, value)

    def get(self, key, default=None):
        return self._first.get(key, default)

    def to_dict(self, flat: bool = True) -> dict:
        # Only flat=True is used; the pages never read repeated values.
        return dict(self._first)


class _Headers:
    """Case-insensitive header view (``.get`` only)."""

    def __init__(self, mapping=None) -> None:
        self._ci = {str(k).lower(): v for k, v in (mapping or {}).items()}

    def get(self, key, default=None):
        return self._ci.get(str(key).lower(), default)


class RequestCtx:
    def __init__(self, query_string: str = "", headers=None) -> None:
        self.args = _Args(query_string)
        self.headers = _Headers(headers)


_EMPTY = RequestCtx()
_current: contextvars.ContextVar[RequestCtx] = contextvars.ContextVar(
    "reqctx", default=_EMPTY
)


class _RequestProxy:
    """Neutral stand-in for ``flask.request`` — resolves to the bound context."""

    @property
    def args(self) -> _Args:
        return _current.get().args

    @property
    def headers(self) -> _Headers:
        return _current.get().headers


request = _RequestProxy()


@contextlib.contextmanager
def bind(query_string: str = "", headers=None):
    """Bind a neutral request context for the duration of the block.

    ``query_string`` is the raw (percent-encoded) ASGI query string; ``headers``
    is an optional mapping (only ``Accept-Language`` is read today).
    """
    token = _current.set(RequestCtx(query_string, headers))
    try:
        yield
    finally:
        _current.reset(token)
