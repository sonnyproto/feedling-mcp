"""Enclave server-side perf knobs:

A. The enclave→backend HTTP hop reuses ONE pooled keep-alive client instead of
   opening a fresh ``httpx.Client`` per call (DNS+TCP+TLS on every whoami/fetch).
C. The gunicorn worker count is env-driven (``FEEDLING_ENCLAVE_WORKERS``) so prod
   (8 vCPU) can parallelize GIL-bound decrypts across processes; default 1 keeps
   the historical single-worker cache-coherent behavior.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import enclave_app  # noqa: E402


# ---- A: pooled enclave→backend client ----


def test_http_client_is_singleton():
    assert enclave_app._http_client() is enclave_app._http_client()


def test_flask_get_headers_reuses_pooled_client_no_per_call_client(monkeypatch):
    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True}

    class _FakeClient:
        def __init__(self):
            self.gets = []

        def get(self, url, params=None, headers=None):
            self.gets.append(url)
            return _FakeResp()

    fake = _FakeClient()
    monkeypatch.setattr(enclave_app, "_http_client", lambda: fake, raising=False)

    def _boom(*a, **k):
        raise AssertionError("must not open a per-call httpx.Client")

    monkeypatch.setattr(enclave_app.httpx, "Client", _boom)

    out1 = enclave_app._flask_get_headers("/v1/a", {"X-API-Key": "k"})
    out2 = enclave_app._flask_get_headers("/v1/b", {"X-API-Key": "k"})
    assert out1 == {"ok": True} and out2 == {"ok": True}
    assert fake.gets == [f"{enclave_app.FLASK_URL}/v1/a", f"{enclave_app.FLASK_URL}/v1/b"]


# ---- C: env-driven worker count ----


def test_gunicorn_options_worker_count_from_env(monkeypatch):
    monkeypatch.setenv("FEEDLING_ENCLAVE_WORKERS", "4")
    assert enclave_app._gunicorn_options(None)["workers"] == 4


def test_gunicorn_options_defaults_to_one_worker(monkeypatch):
    monkeypatch.delenv("FEEDLING_ENCLAVE_WORKERS", raising=False)
    assert enclave_app._gunicorn_options(None)["workers"] == 1


def test_gunicorn_options_preserves_thread_and_bind(monkeypatch):
    monkeypatch.delenv("FEEDLING_ENCLAVE_WORKERS", raising=False)
    opts = enclave_app._gunicorn_options(None)
    assert opts["worker_class"] == "gthread"
    assert opts["threads"] == enclave_app._ENCLAVE_THREADS
    assert opts["bind"] == f"0.0.0.0:{enclave_app.ENCLAVE_PORT}"
