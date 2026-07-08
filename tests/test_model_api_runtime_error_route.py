"""POST /v1/model_api/runtime_error：agent-runner 路径补写 last_runtime_error。

历史教训（memory: model-api-providerkey-runtime-token-decrypt-gap）：host-all
consumer 只有 runtime-token，读写侧只认 api-key 就会静默失效——这里走
require_auth（两者都收），测试只需覆盖 api-key 路径 + 语义。
Run:  python -m pytest tests/test_model_api_runtime_error_route.py -q
"""
from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import asgi_app  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import config_store  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def user(backend_env):
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    uid, key = body["user_id"], body["api_key"]
    # record_runtime_error patches the model_api_runtime profile, which only
    # exists once model_api is configured (see _ensure_model_api_runtime_profile:
    # it bails to None when _load_model_api_config(store) is empty). Registering
    # a bare user does not configure model_api, so seed a minimal config here —
    # otherwise the route 404s with model_api_runtime_profile_missing before the
    # semantics under test (truncation / clearing) ever run.
    store = core_store.get_store(uid)
    config_store._save_model_api_config(store, {"provider": "anthropic", "model": "claude-3-5-sonnet-latest"})
    config_store._ensure_model_api_runtime_profile(store)
    return uid, key


def _post(path, *, headers=None, json=None):
    async def go():
        transport = httpx.ASGITransport(app=asgi_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(path, headers=headers or {}, json=json)
            return r.status_code, r.json()
    return asyncio.run(go())


def _runtime_profile(user_id):
    store = core_store.get_store(user_id)
    return config_store._ensure_model_api_runtime_profile(store) or {}


def test_report_and_clear_runtime_error(user):
    uid, key = user
    status, body = _post("/v1/model_api/runtime_error",
                         headers={"X-API-Key": key},
                         json={"error": "403 预扣费额度失败", "error_class": "quota_insufficient"})
    assert status == 200, body
    prof = _runtime_profile(uid)
    assert prof["last_runtime_error"] == "403 预扣费额度失败"

    status, body = _post("/v1/model_api/runtime_error",
                         headers={"X-API-Key": key},
                         json={"error": "", "error_class": ""})
    assert status == 200, body
    assert _runtime_profile(uid)["last_runtime_error"] == ""


def test_error_truncated_to_300(user):
    uid, key = user
    status, _ = _post("/v1/model_api/runtime_error",
                      headers={"X-API-Key": key},
                      json={"error": "x" * 900, "error_class": "k" * 200})
    assert status == 200
    prof = _runtime_profile(uid)
    assert len(prof["last_runtime_error"]) == 300
    assert len(prof["last_runtime_error_class"]) == 64


def test_bad_auth_401():
    status, _ = _post("/v1/model_api/runtime_error",
                      headers={"X-API-Key": "nope"}, json={"error": "e"})
    assert status == 401


def test_missing_profile_404(backend_env):
    """No model_api config configured yet -> record_runtime_error's 404 branch."""
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x22" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    key = res.get_json()["api_key"]

    status, body = _post("/v1/model_api/runtime_error",
                         headers={"X-API-Key": key},
                         json={"error": "boom", "error_class": "x"})
    assert status == 404, body
    assert body["error"] == "model_api_runtime_profile_missing"
