"""Unit tests for screen routes' enclave auth forwarding.

host-all / zero-roster agents authenticate with a Stage-D runtime token and have
no api_key; the enclave-proxy routes must forward that token (not an empty header)
or every hosted-agent screen read fails. Mirrors the memory readside fix.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from flask import Flask  # noqa: E402

from screen import routes as screen_routes  # noqa: E402

_app = Flask(__name__)


def test_enclave_forward_auth_prefers_runtime_token():
    with _app.test_request_context(
        headers={"X-Feedling-Runtime-Token": "rt_1", "X-API-Key": "ak_1"}
    ):
        assert screen_routes._enclave_forward_auth() == {"X-Feedling-Runtime-Token": "rt_1"}


def test_enclave_forward_auth_falls_back_to_api_key():
    with _app.test_request_context(headers={"X-API-Key": "ak_1"}):
        assert screen_routes._enclave_forward_auth() == {"X-API-Key": "ak_1"}


def test_enclave_forward_auth_empty_when_no_credential():
    with _app.test_request_context(headers={}):
        assert screen_routes._enclave_forward_auth() == {}
