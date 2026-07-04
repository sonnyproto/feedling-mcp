"""Shared JSON response + fixed error-body helpers for the ASGI routers.

The error bodies mirror the Flask ``errorhandler`` bodies verbatim (plan §3.1)
so a client sees the same shape regardless of which backend served it — a
hard parity requirement under the no-fallback cutover.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

# Verbatim parity with app.py's errorhandler(401/403/503) bodies (plan §3.1).
ERROR_BODIES: dict[int, dict] = {
    401: {"error": "unauthorized"},
    403: {"error": "forbidden"},
    503: {"error": "service_unavailable", "detail": "admin token is not configured"},
}


def json_error(status_code: int, body: dict) -> JSONResponse:
    return JSONResponse(body, status_code=status_code)
