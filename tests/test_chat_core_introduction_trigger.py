"""chat_core.verify_loop route filter for the resident first-greeting trigger
(Codex review: prove the wiring, not just the helper).

``_maybe_enqueue_resident_introduction`` is the block verify_loop runs after a
successful chat_loop_verified. It must enqueue ONLY on the resident route, and a
failing enqueue must never propagate (best-effort — it must not fail the verify
response). These are pure wiring tests (no DB): route + dispatch + error
swallowing. The real DB exactly-once is in test_introduction_db_atomic.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from chat import chat_core


class _Store:
    user_id = "usr_wiring"


def _patch(monkeypatch, route, enqueue):
    monkeypatch.setattr("accounts.onboarding._load_onboarding_route", lambda store: route)
    monkeypatch.setattr("agent_runtime.introduction.enqueue_introduction_once", enqueue)


def test_resident_route_enqueues(monkeypatch):
    calls = []
    _patch(monkeypatch, "resident", lambda store, *, now=None: calls.append(store) or {"job_id": "x"})
    chat_core._maybe_enqueue_resident_introduction(_Store())
    assert len(calls) == 1


def test_model_api_route_does_not_enqueue(monkeypatch):
    calls = []
    _patch(monkeypatch, "model_api", lambda store, *, now=None: calls.append(store))
    chat_core._maybe_enqueue_resident_introduction(_Store())
    assert calls == []


def test_official_import_route_does_not_enqueue(monkeypatch):
    calls = []
    _patch(monkeypatch, "official_import", lambda store, *, now=None: calls.append(store))
    chat_core._maybe_enqueue_resident_introduction(_Store())
    assert calls == []


def test_enqueue_failure_is_swallowed(monkeypatch):
    def boom(store, *, now=None):
        raise RuntimeError("db down")
    _patch(monkeypatch, "resident", boom)
    # Must NOT raise — a failed enqueue can't be allowed to fail the verify response.
    chat_core._maybe_enqueue_resident_introduction(_Store())
