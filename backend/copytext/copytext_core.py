"""Framework-neutral /v1/copytext read + admin-edit logic (ASGI-migration §5.3).

Lifted out of the Flask route so the native ASGI route (copytext.routes_asgi)
reuses the exact same payload / status shape — no Flask or FastAPI request object
crosses in here. The caller parses the request (If-None-Match header, JSON body)
and passes the copytext ``store`` module + already-parsed values; each function
returns ``{"status", "body", ...}`` so both routes render byte-identical output.

Both the read (``build_bundle`` / ``get_revision``) and the write
(``apply_edits``) touch the DB pool, so ASGI callers must run these on the
threadpool (``asgi.threadpool.run_db``), never directly on the event loop.
"""

from __future__ import annotations

from . import service


def _etag(revision: int) -> str:
    # Weak-free strong ETag; quoted per RFC 7232 (mirrors routes._etag).
    return f'"{revision}"'


def copytext_get_payload(store, *, if_none_match: str = "") -> dict:
    """Build the GET /v1/copytext result.

    Returns ``{"status": 200|304, "etag": '"<rev>"', "body": {...}}``. On an
    If-None-Match hit the body is ``{}`` and status 304; otherwise the full
    bundle with an ETag derived from the freshly built bundle's revision — the
    exact two-revision-read behavior of the Flask route.
    """
    revision = store.get_revision()
    etag = _etag(revision)
    if (if_none_match or "").strip() == etag:
        return {"status": 304, "etag": etag, "body": {}}
    bundle = service.build_bundle()
    return {"status": 200, "etag": _etag(bundle["revision"]), "body": bundle}


def copytext_post(store, *, body) -> dict:
    """Apply an admin edit (validated + persisted via the copytext service).

    ``body`` is the already-parsed JSON payload (``store`` is the copytext store
    the service writes through). Returns ``{"status": 200, "body": <summary>}``
    on success, or ``{"status": 400, "body": {"error": "invalid_payload",
    "detail": ...}}`` on a malformed payload — identical to the Flask route.
    """
    payload = body or {}
    try:
        result = service.apply_edits(payload)
    except service.CopytextValidationError as e:
        return {"status": 400, "body": {"error": "invalid_payload", "detail": str(e)}}
    return {"status": 200, "body": result}
