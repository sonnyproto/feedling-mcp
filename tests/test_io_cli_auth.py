"""io_cli credential resolution.

The hosted agent invokes ``io_cli.py perception`` as a Bash tool. In zero-roster
host-all mode the spawned env has NO ``FEEDLING_API_KEY`` — the consumer (and so
its tools) must authenticate with the Stage-D runtime token written to
``FEEDLING_RUNTIME_TOKEN_FILE`` instead, or perception calls would 401.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import io_cli  # noqa: E402


def test_auth_headers_prefers_api_key(monkeypatch):
    monkeypatch.setenv("FEEDLING_API_KEY", "k")
    monkeypatch.delenv("FEEDLING_RUNTIME_TOKEN_FILE", raising=False)
    assert io_cli._auth_headers() == {"X-API-Key": "k"}


def test_auth_headers_falls_back_to_runtime_token(tmp_path, monkeypatch):
    monkeypatch.delenv("FEEDLING_API_KEY", raising=False)
    tf = tmp_path / "runtime-token"
    tf.write_text("tok.sig\n")
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_FILE", str(tf))
    assert io_cli._auth_headers() == {"X-Feedling-Runtime-Token": "tok.sig"}


def test_auth_headers_empty_when_neither(monkeypatch):
    monkeypatch.delenv("FEEDLING_API_KEY", raising=False)
    monkeypatch.delenv("FEEDLING_RUNTIME_TOKEN_FILE", raising=False)
    assert io_cli._auth_headers() == {}


def test_auth_headers_empty_when_token_file_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("FEEDLING_API_KEY", raising=False)
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_FILE", str(tmp_path / "nope"))
    assert io_cli._auth_headers() == {}
