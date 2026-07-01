"""Backend gunicorn worker count is env-driven (``FEEDLING_BACKEND_WORKERS``,
default 1). The code already supports ``-w N`` (LISTEN/NOTIFY wake bus +
advisory-lock leader election for the :9998 bind), so lifting the worker count
off the pinned ``-w 1`` is a config/env change, not new machinery."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import gunicorn_conf  # noqa: E402


def test_worker_count_from_env(monkeypatch):
    monkeypatch.setenv("FEEDLING_BACKEND_WORKERS", "3")
    assert gunicorn_conf._worker_count() == 3


def test_worker_count_defaults_to_one(monkeypatch):
    monkeypatch.delenv("FEEDLING_BACKEND_WORKERS", raising=False)
    assert gunicorn_conf._worker_count() == 1


def test_worker_count_clamped_to_at_least_one(monkeypatch):
    monkeypatch.setenv("FEEDLING_BACKEND_WORKERS", "0")
    assert gunicorn_conf._worker_count() == 1
