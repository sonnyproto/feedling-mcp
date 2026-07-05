from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402

from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave.routes import build_app  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    return _AsgiTestClient(build_app())


def test_healthz_ready(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "ready": True}


def test_healthz_not_ready(monkeypatch, client):
    monkeypatch.setitem(enclave_state._state, "ready", False)
    monkeypatch.setitem(enclave_state._state, "error", "boom")
    r = client.get("/healthz")
    assert r.status_code == 503
    assert r.get_json() == {"ok": False, "ready": False, "error": "boom"}


def test_attestation_shape_and_cache_header(monkeypatch, client):
    monkeypatch.setitem(enclave_state._state, "content_pk_hex", "aa" * 32)
    monkeypatch.setitem(enclave_state._state, "signing_pk_hex", "bb" * 32)
    monkeypatch.setitem(enclave_state._state, "booted_at", 123.0)
    monkeypatch.setitem(enclave_state._state, "attestation", {
        "tdx_quote_hex": "cc" * 64, "event_log_json": "[]",
        "measurements": {"mrtd": "00"}, "compose_hash": "h",
        "app_id": "app", "instance_id": "inst",
    })
    r = client.get("/attestation")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=60"
    body = r.get_json()
    for field in ("tdx_quote_hex", "enclave_content_pk_hex", "enclave_release",
                  "app_auth", "report_data_version", "phase", "tls_in_enclave",
                  "mcp_tls_cert_pubkey_fingerprint_hex", "notes", "booted_at"):
        assert field in body


def test_attestation_not_ready(monkeypatch, client):
    monkeypatch.setitem(enclave_state._state, "ready", False)
    monkeypatch.setitem(enclave_state._state, "error", "kms down")
    r = client.get("/attestation")
    assert r.status_code == 503
    assert r.get_json() == {"error": "not_ready", "detail": "kms down"}


def test_gzip_on_large_response(monkeypatch, client):
    monkeypatch.setitem(enclave_state._state, "content_pk_hex", "aa" * 32)
    monkeypatch.setitem(enclave_state._state, "signing_pk_hex", "bb" * 32)
    monkeypatch.setitem(enclave_state._state, "booted_at", 1.0)
    monkeypatch.setitem(enclave_state._state, "attestation", {
        "tdx_quote_hex": "ab" * 8000,  # 16KB，远超 500B 阈值
        "event_log_json": "[]", "measurements": {}, "compose_hash": "h",
        "app_id": "a", "instance_id": "i",
    })
    r = client.get("/attestation", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    assert len(r.get_json()["tdx_quote_hex"]) == 16000  # httpx 已自动解压


def test_head_and_options_behavior(client):
    # HEAD：Starlette 对 GET 路由自动支持（探活工具依赖，锁定）。
    assert client.open("/healthz", method="HEAD").status_code == 200
    # OPTIONS：有意偏差（Global Constraints #1）—— Flask 自动 200，新栈 405+Allow。
    r = client.open("/healthz", method="OPTIONS")
    assert r.status_code == 405
    assert "GET" in r.headers.get("allow", "")
