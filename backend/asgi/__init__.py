"""ASGI/FastAPI backend package (ASGI-migration plan §5.1).

Framework glue for the native FastAPI backend — settings, the bounded
sync->thread bridge, response/error helpers, the access-log + exception
middleware, and the startup/shutdown lifespan. Business logic never lives here;
it stays in the domain packages, exactly as ``app.py`` assembly stays thin for
Flask (CONTRIBUTING §1).

Nothing in this package may ``import app`` (the Flask assembly / parity oracle) —
see ``asgi_app`` and the CI guard test.
"""
