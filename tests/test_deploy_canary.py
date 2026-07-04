"""§3 deploy canary self-clean guarantee: a passing round-trip whose account
reset fails must still exit non-zero (never leave the throwaway account behind).
Codex review finding — tools/deploy_canary.py.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import deploy_canary as dc  # noqa: E402

# A real X25519 pk (the live prod enclave content pk) so from_public_bytes works.
PK = "2d642ec1f54719d8c6088e8cbaf394961cb804a533bd4d7366d48d1d543f5620"


@pytest.fixture()
def canary_env(monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", "testsha00000")
    monkeypatch.setattr(dc, "SHA", "testsha00000")
    monkeypatch.setattr(dc, "API_URL", "https://api.test")
    monkeypatch.setattr(dc, "ENCLAVE_URL", "https://enclave.test")
    monkeypatch.setattr(dc, "DECRYPT_RETRIES", 1)
    # Deterministic item_id so the decrypt stub can echo the exact plaintext.
    monkeypatch.setattr(dc.secrets, "token_hex", lambda n: "ab" * n)


def _stub_http(reset_status: int):
    item_id = "ab" * 16
    plaintext = f"deploy-canary testsha00000 {item_id}".encode()
    pt_b64 = base64.b64encode(plaintext).decode()

    def _http(method, url, *, body=None, api_key=None, insecure=False, timeout=30.0):
        if url.endswith("/attestation"):
            return 200, {"enclave_content_pk_hex": PK}
        if url.endswith("/v1/users/register"):
            return 201, {"user_id": "usr_canary", "api_key": "k"}
        if url.endswith("/v1/users/whoami"):
            return 200, {"enclave_content_public_key_hex": PK}
        if url.endswith("/v1/envelope/decrypt"):
            return 200, {"plaintext_b64": pt_b64}
        if url.endswith("/v1/account/reset"):
            return reset_status, {}
        raise AssertionError(f"unexpected url {url}")
    return _http


def test_reset_failure_fails_the_canary(canary_env, monkeypatch, capsys):
    monkeypatch.setattr(dc, "_http", _stub_http(reset_status=403))
    with pytest.raises(SystemExit) as e:
        dc.main()
    assert e.value.code == 1
    assert "CANARY OK" not in capsys.readouterr().out


def test_clean_run_passes(canary_env, monkeypatch, capsys):
    monkeypatch.setattr(dc, "_http", _stub_http(reset_status=200))
    dc.main()  # no SystemExit
    assert "CANARY OK" in capsys.readouterr().out
