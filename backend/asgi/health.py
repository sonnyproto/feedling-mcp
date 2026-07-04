"""Native /healthz router — the first ASGI route (plan §8 first native routes).

Parity with Flask ``content.healthz``: same public, no-auth liveness/readiness
probe, same body. Lives in ``asgi/`` for now; later PRs move routes into each
domain package's ``routes_asgi.py`` exposing ``register_asgi(app)`` (plan §5.3).
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/healthz")
async def healthz():
    """Liveness + readiness probe. Public, no auth — used by Docker/compose."""
    return {"ok": True, "mode": "multi_tenant"}
