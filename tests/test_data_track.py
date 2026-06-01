from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import app as appmod  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "FEEDLING_DIR", tmp_path)
    monkeypatch.setattr(appmod, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", "admin-test-token")
    appmod._users[:] = []
    appmod._key_to_user.clear()
    appmod._stores.clear()
    appmod._save_users()
    appmod.app.config.update(TESTING=True)
    with appmod.app.test_client() as c:
        yield c


def _register(client) -> tuple[str, str]:
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def _admin_headers() -> dict[str, str]:
    return {"X-Admin-Token": "admin-test-token"}


def _env(msg_id: str, user_id: str) -> dict:
    return {
        "id": msg_id,
        "v": 1,
        "body_ct": "ciphertext-that-must-not-leak",
        "nonce": "nonce-that-must-not-leak",
        "K_user": "wrapped-user-key",
        "K_enclave": "wrapped-enclave-key",
        "visibility": "shared",
        "owner_user_id": user_id,
    }


def test_track_event_scrubs_sensitive_payload(client):
    user_id, api_key = _register(client)

    res = client.post(
        "/v1/track/event",
        headers=_headers(api_key),
        json={
            "type": "onboarding_skill_copied",
            "route": "resident",
            "app_version": "1.0",
            "build": "42",
            "payload": {
                "screen": "chat_empty",
                "characters": 123,
                "prompt": "private prompt",
                "api_key": "sk-private",
                "file_name": "private.txt",
                "nested": {"step": "skill", "token": "private-token"},
            },
        },
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    events = appmod.get_store(user_id).list_tracking_events(limit=0)
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["screen"] == "chat_empty"
    assert payload["characters"] == 123
    assert payload["nested"] == {"step": "skill"}
    assert "prompt" not in payload
    assert "api_key" not in payload
    assert "file_name" not in payload
    assert "private" not in json.dumps(events[0])


def test_admin_data_track_requires_admin_token(client, monkeypatch):
    _register(client)

    no_token = client.get("/v1/admin/data-track/users")
    assert no_token.status_code == 401

    good = client.get("/v1/admin/data-track/users", headers=_admin_headers())
    assert good.status_code == 200

    monkeypatch.delenv("FEEDLING_ADMIN_TOKEN")
    disabled = client.get("/v1/admin/data-track/users", headers=_admin_headers())
    assert disabled.status_code == 503


def test_admin_data_track_aggregates_counts_without_content(client):
    user_id, api_key = _register(client)
    store = appmod.get_store(user_id)

    store.append_chat("user", "chat", _env("msg_user_1", user_id))
    store.append_chat("openclaw", "chat", _env("msg_agent_1", user_id))
    store.append_chat(
        "openclaw",
        appmod.PROACTIVE_JOB_SOURCE,
        _env("msg_proactive_1", user_id),
        extra={
            "proactive_job_id": "pj_1",
            "live_activity_status": "delivered",
            "alert_status": "delivered",
            "alert_preview": "private alert preview",
        },
    )
    appmod._save_moments(
        store,
        [
            {"id": "mem_1", "type": "moment", "source": "bootstrap", "created_at": "2026-06-01T01:00:00"},
            {"id": "mem_2", "type": "fact", "source": "chat", "created_at": "2026-06-01T02:00:00"},
        ],
    )
    store.identity_file.write_text(json.dumps({
        "updated_at": "2026-06-01T03:00:00",
        "relationship_started_at": "2026-06-01",
        "relationship_anchor_evidence": "private evidence",
    }))
    store.append_tracking_event(appmod._make_tracking_event(
        store,
        "onboarding_connection_copied",
        {"payload": {"screen": "chat_empty", "prompt": "private copied prompt"}},
    ))

    res = client.get("/v1/admin/data-track/users", headers=_admin_headers())

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["summary"]["users_total"] == 1
    assert body["summary"]["chat_messages_total"] == 3
    assert body["summary"]["memory_total"] == 2
    row = body["users"][0]
    assert row["chat"]["total"] == 3
    assert row["chat"]["user_messages"] == 1
    assert row["chat"]["agent_messages"] == 2
    assert row["memory"]["by_tab"]["story"] == 1
    assert row["memory"]["by_tab"]["about_me"] == 1
    assert row["proactive"]["proactive_messages"] == 1
    dumped = json.dumps(body)
    assert "ciphertext-that-must-not-leak" not in dumped
    assert "private alert preview" not in dumped
    assert "private copied prompt" not in dumped
    assert "private evidence" not in dumped
