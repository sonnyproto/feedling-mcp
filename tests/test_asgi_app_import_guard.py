"""CI guard: ``import asgi_app`` must NOT import the Flask ``app`` (plan §5.3/§16).

``app.py`` is the parity oracle; importing it re-triggers all of its import-time
side effects (db.init_schema, wake_bus listener, WS-leader :9998 bind) and
smuggles the old startup chain back into the ASGI process. This must run in a
FRESH interpreter — the pytest process already imported ``app`` via conftest, so
we assert in a subprocess.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"


def test_importing_asgi_app_does_not_import_flask_app():
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(BACKEND)!r})\n"
        "import asgi_app\n"
        "assert hasattr(asgi_app, 'app'), 'asgi_app.app missing'\n"
        "assert 'app' not in sys.modules, 'asgi_app pulled in Flask app.py'\n"
        "print('GUARD_OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    assert "GUARD_OK" in proc.stdout
