"""FastAPI backend assembly (ASGI-migration plan §5.3).

The ASGI counterpart of ``app.py`` — **assembly only**, no business logic
(inherits CONTRIBUTING §1's app.py discipline). It wires the lifespan, the
access-log middleware, the fixed-body exception handlers, and includes the
native routers.

HARD RULE: this module (and anything it imports) must **never** ``import app``.
``app.py`` is the Flask parity oracle; importing it would re-trigger all of its
import-time side effects (db.init_schema, wake_bus listener, WS leader bind) and
smuggle the old startup chain back in. A CI guard test asserts ``app`` is not in
``sys.modules`` after ``import asgi_app``.

Until every route is migrated, an unmigrated path is simply a 404 here — the
expected behavior when validating the parallel :5005 instance, and the reason
the url_map ledger (docs/BACKEND_ASGI_ROUTE_MATRIX.md) must hit 100% before
cutover.

Start command (plan §5.2):
    gunicorn --chdir backend --config backend/gunicorn_conf.py \\
        -k asgi.worker.FeedlingUvicornWorker --timeout 120 \\
        -b 0.0.0.0:5001 asgi_app:app
"""

from __future__ import annotations

import importlib

from fastapi import FastAPI

from asgi import health, middleware
from asgi.lifespan import lifespan

# Domain packages exposing register_asgi(app), the ASGI counterpart of app.py's
# pkg.register(app). Added as each package's routes_asgi.py lands (plan §5.3).
# Assembly-only: names live here, logic lives in the packages.
_ASGI_PACKAGES = (
    "accounts.routes_asgi",
    "bootstrap.routes_asgi",
    "chat.routes_asgi",
    "proactive.routes_asgi",
    "agent.routes_asgi",
    "copytext.routes_asgi",
    "tracking.routes_asgi",
    "push.routes_asgi",
    "diagnostics.routes_asgi",
    "worldbook.routes_asgi",
    "identity.routes_asgi",
    "memory.routes_asgi",
    "screen.routes_asgi",
    "content.routes_asgi",
    "perception.routes_asgi",
    "admin.routes_asgi",
    "genesis.routes_asgi",
    "onboarding_archive.routes_asgi",
    "hosted.setup_routes_asgi",
    "hosted.chat_routes_asgi",
    "hosted.history_import_asgi",
    "hosted.onboarding_validation_asgi",
)

# Disable the auto OpenAPI/docs routes so the ASGI route surface is exactly the
# migrated routes — nothing outside the url_map ledger, which keeps the
# post-cutover 404 monitoring (plan §15.1) meaningful.
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

# Outermost: structured access log + ?key= redaction + cancelled-request lines.
app.add_middleware(middleware.AccessLogMiddleware)

# Fixed-body error mapping (parity with app.py errorhandler 401/403/503).
middleware.register_exception_handlers(app)

# Native routers. /healthz has no auth/package; domain routers fan out via
# register_asgi(app), mirroring app.py's pkg.register(app).
app.include_router(health.router)
for _mod_name in _ASGI_PACKAGES:
    importlib.import_module(_mod_name).register_asgi(app)
