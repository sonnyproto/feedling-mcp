"""account_reset (POST /v1/account/reset) 必须清理该用户的 onboarding R2 归档。

行为测试：patch onboarding_archive.storage.delete_user_archives 为记录器，
走真实重置路由，断言它被以正确 user_id 调到。
"""

import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import app as appmod  # noqa: E402
from core import config as core_config  # noqa: E402
import onboarding_archive.storage as oa_storage  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    appmod._users[:] = []
    appmod._key_to_user.clear()
    appmod._stores.clear()
    appmod._save_users()
    appmod.app.config.update(TESTING=True)
    with appmod.app.test_client() as c:
        yield c


_pk_counter = itertools.count(1)


def _register(client) -> tuple[str, str]:
    import base64
    raw = next(_pk_counter).to_bytes(32, "big")
    res = client.post(
        "/v1/users/register",
        json={"public_key": base64.b64encode(raw).decode(), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def test_reset_purges_onboarding_archives_when_enabled(client, monkeypatch):
    uid, api_key = _register(client)
    calls = []
    monkeypatch.setattr(oa_storage, "enabled", lambda: True)
    monkeypatch.setattr(oa_storage, "delete_user_archives", lambda u: calls.append(u))
    res = client.post("/v1/account/reset",
                      json={"confirm": "delete-all-data"},
                      headers={"X-API-Key": api_key})
    assert res.status_code == 200, res.get_data(as_text=True)
    assert calls == [uid]


def test_reset_skips_archive_cleanup_when_disabled(client, monkeypatch):
    uid, api_key = _register(client)
    calls = []
    monkeypatch.setattr(oa_storage, "enabled", lambda: False)
    monkeypatch.setattr(oa_storage, "delete_user_archives", lambda u: calls.append(u))
    res = client.post("/v1/account/reset",
                      json={"confirm": "delete-all-data"},
                      headers={"X-API-Key": api_key})
    assert res.status_code == 200, res.get_data(as_text=True)
    assert calls == []  # enabled() False → 不调用


def test_reset_aborts_when_archive_cleanup_fails(client, monkeypatch):
    """R2 清理失败时重置必须中止并返回 503，账号不得被删除（P1 修复）。"""
    uid, api_key = _register(client)

    def _boom(_u):
        raise RuntimeError("r2 delete failed")

    monkeypatch.setattr(oa_storage, "enabled", lambda: True)
    monkeypatch.setattr(oa_storage, "delete_user_archives", _boom)
    res = client.post("/v1/account/reset",
                      json={"confirm": "delete-all-data"},
                      headers={"X-API-Key": api_key})
    assert res.status_code == 503, res.get_data(as_text=True)
    assert res.get_json()["error"] == "archive_cleanup_failed"
    # 中止后账号未被删：用同一 key 再次请求仍能通过鉴权（不是 401）
    res2 = client.post("/v1/account/reset",
                       json={"confirm": "wrong"},
                       headers={"X-API-Key": api_key})
    assert res2.status_code == 400  # 通过鉴权、卡在 confirm 校验 → 证明用户还在
