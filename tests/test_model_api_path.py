from __future__ import annotations

import base64
import os
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
    appmod._users[:] = []
    appmod._key_to_user.clear()
    appmod._stores.clear()
    appmod._save_users()
    monkeypatch.setattr(
        appmod,
        "_get_enclave_info",
        lambda: {"content_pk_hex": ("22" * 32), "compose_hash": "test"},
    )
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


def _identity_payload() -> dict:
    names = ["Attentive", "Steady", "Playful", "Protective", "Curious", "Direct", "Tender"]
    return {
        "agent_name": "IO",
        "self_introduction": "I imported the history and can now answer with context.",
        "category": "Attentive · Grounded",
        "signature": ["Built from receipts", "Ready to keep noticing"],
        "dimensions": [
            {"name": name, "value": 50 + idx, "description": f"Grounded dimension {idx}"}
            for idx, name in enumerate(names)
        ],
    }


def test_model_api_setup_encrypts_and_redacts(client, monkeypatch):
    user_id, api_key = _register(client)
    raw_provider_key = "sk-test-secret"

    monkeypatch.setattr(
        appmod,
        "test_provider_key",
        lambda cfg: {"reply": "ok", "usage": {"total_tokens": 1}},
    )

    route_res = client.post(
        "/v1/onboarding/route",
        json={"route": "model_api"},
        headers=_headers(api_key),
    )
    assert route_res.status_code == 200

    setup = client.post(
        "/v1/model_api/setup",
        json={
            "provider": "openrouter",
            "model": "openai/gpt-4.1-mini",
            "api_key": raw_provider_key,
        },
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)
    public = setup.get_json()["config"]
    assert public["configured"] is True
    assert public["provider"] == "openrouter"
    assert "api_key" not in public
    assert "api_key_envelope" not in public

    get_res = client.get("/v1/model_api/get", headers=_headers(api_key))
    assert get_res.status_code == 200
    assert "api_key_envelope" not in get_res.get_json()["config"]

    config_text = (appmod.FEEDLING_DIR / user_id / "model_api.json").read_text()
    assert raw_provider_key not in config_text
    assert "api_key_envelope" in config_text

    validate = client.get("/v1/onboarding/validate", headers=_headers(api_key))
    assert validate.status_code == 200
    body = validate.get_json()
    assert body["route"] == "model_api"
    assert body["stage"] == "history_import"
    assert all(step["id"] != "resident_consumer" for step in body["steps"])


def test_model_api_setup_can_reuse_saved_key_when_model_changes(client, monkeypatch):
    _, api_key = _register(client)
    calls = []

    def fake_test_provider_key(cfg):
        calls.append((cfg.provider, cfg.model, cfg.api_key, cfg.base_url))
        return {"reply": "ok", "usage": {"total_tokens": 1}}

    monkeypatch.setattr(appmod, "test_provider_key", fake_test_provider_key)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-existing"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)
    first = setup.get_json()["config"]

    monkeypatch.setattr(
        appmod,
        "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: b"sk-existing",
    )
    update = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1"},
        headers=_headers(api_key),
    )
    assert update.status_code == 200, update.get_data(as_text=True)
    second = update.get_json()["config"]
    assert second["provider"] == "openai"
    assert second["model"] == "gpt-4.1"
    assert second["api_key_hint"] == first["api_key_hint"]
    assert calls[-1] == ("openai", "gpt-4.1", "sk-existing", "https://api.openai.com/v1")


def test_history_import_and_hosted_chat_complete_model_api_path(client, monkeypatch):
    user_id, api_key = _register(client)

    monkeypatch.setattr(
        appmod,
        "test_provider_key",
        lambda cfg: {"reply": "ok", "usage": {"total_tokens": 1}},
    )
    monkeypatch.setattr(
        appmod,
        "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: b"sk-test-secret",
    )

    def fake_chat_completion(cfg, messages, **kwargs):
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        if "Extract Memory Garden" in joined:
            return {
                "reply": (
                    '{"memories":['
                    '{"type":"moment","title":"First import moment","description":"User shared a concrete concern.","occurred_at":"2026-05-31"},'
                    '{"type":"fact","title":"User preference","description":"User prefers direct answers.","occurred_at":"2026-05-31"}'
                    "]}"
                ),
                "usage": {},
            }
        if "Derive a Feedling Identity Card" in joined:
            return {"reply": appmod.json.dumps(_identity_payload()), "usage": {}}
        return {"reply": "I can answer from the imported history now.", "usage": {"total_tokens": 12}}

    monkeypatch.setattr(appmod, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test-secret"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    transcript = "\n".join([
        "2026-05-31 User: I prefer direct answers and want to test the API route.",
        "2026-05-31 Assistant: I will keep replies direct and grounded.",
    ])
    upload = client.post(
        "/v1/history_import/upload",
        json={
            "format": "plaintext",
            "content": transcript,
            "relationship_started_at": "2026-05-31",
        },
        headers=_headers(api_key),
    )
    assert upload.status_code == 201, upload.get_data(as_text=True)
    job = upload.get_json()["job"]
    assert job["status"] == "completed"
    assert job["messages_parsed"] == 2
    assert job["memories_created"] >= 2
    assert job["identity_written"] is True

    mid_validate = client.get("/v1/onboarding/validate", headers=_headers(api_key)).get_json()
    assert mid_validate["route"] == "model_api"
    assert mid_validate["stage"] == "hosted_chat"

    def fake_enclave_context(path, key, params=None):
        if path == "/v1/chat/history":
            return {
                "messages": [
                    {"role": "user", "content": "I prefer direct answers.", "source": "history_import"},
                ],
                "context_memories": [
                    {"title": "User preference", "description": "User prefers direct answers."},
                ],
            }, ""
        if path == "/v1/identity/get":
            return {"identity": _identity_payload()}, ""
        return {}, ""

    monkeypatch.setattr(appmod, "_enclave_get_json_for_gate", fake_enclave_context)

    chat = client.post(
        "/v1/model_api/chat/send",
        json={"message": "Can you reply using my imported history?"},
        headers=_headers(api_key),
    )
    assert chat.status_code == 200, chat.get_data(as_text=True)
    chat_body = chat.get_json()
    assert chat_body["reply"] == "I can answer from the imported history now."
    assert chat_body["context"]["identity_loaded"] is True
    assert chat_body["context"]["memories"] == 1

    final_validate = client.get("/v1/onboarding/validate", headers=_headers(api_key)).get_json()
    assert final_validate["passing"] is True
    assert final_validate["stage"] == "complete"

    history = client.get("/v1/chat/history?limit=20", headers=_headers(api_key))
    assert history.status_code == 200
    rows = history.get_json()["messages"]
    assert any(row["source"] == "model_api" and row["role"] == "user" for row in rows)
    assert any(row["source"] == "model_api" and row["role"] == "openclaw" for row in rows)
    assert all("body_ct" in row for row in rows if row["source"] == "model_api")
    assert "sk-test-secret" not in (appmod.FEEDLING_DIR / user_id / "model_api.json").read_text()


def test_history_import_accepts_json_file_and_persona_profile(client, monkeypatch):
    user_id, api_key = _register(client)

    monkeypatch.setattr(
        appmod,
        "test_provider_key",
        lambda cfg: {"reply": "ok", "usage": {"total_tokens": 1}},
    )
    monkeypatch.setattr(
        appmod,
        "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: b"sk-test-secret",
    )

    def fake_chat_completion(cfg, messages, **kwargs):
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        if "Extract Memory Garden" in joined:
            assert "Long-term user profile" in joined
            return {
                "reply": (
                    '{"memories":['
                    '{"type":"moment","title":"Imported JSON moment","description":"User tested JSON export import.","occurred_at":"2026-05-30"},'
                    '{"type":"fact","title":"Persona preference","description":"User likes durable setup context.","occurred_at":"2026-05-30"}'
                    "]}"
                ),
                "usage": {},
            }
        if "Derive a Feedling Identity Card" in joined:
            return {"reply": appmod.json.dumps(_identity_payload()), "usage": {}}
        return {"reply": "ok", "usage": {}}

    monkeypatch.setattr(appmod, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test-secret"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    chat_export = {
        "messages": [
            {
                "role": "user",
                "content": "I am testing JSON history import.",
                "created_at": "2026-05-30T08:00:00",
            },
            {
                "role": "assistant",
                "content": [{"text": "I will preserve context."}],
                "created_at": "2026-05-30T08:01:00",
            },
        ]
    }
    upload = client.post(
        "/v1/history_import/upload",
        json={
            "format": "auto",
            "content": appmod.json.dumps(chat_export),
            "history_filename": "chat-export.json",
            "persona_content": "Long-term user profile: prefers durable setup context.",
            "persona_filename": "persona.md",
        },
        headers=_headers(api_key),
    )
    assert upload.status_code == 201, upload.get_data(as_text=True)
    job = upload.get_json()["job"]
    assert job["status"] == "completed"
    assert job["messages_parsed"] == 2
    assert job["support_materials"] == 1
    assert job["history_filename"] == "chat-export.json"
    assert job["persona_filename"] == "persona.md"
    assert job["memories_created"] >= 2
    assert job["identity_written"] is True


def test_history_import_allows_confirmed_fresh_start_without_materials(client, monkeypatch):
    user_id, api_key = _register(client)

    monkeypatch.setattr(
        appmod,
        "test_provider_key",
        lambda cfg: {"reply": "ok", "usage": {"total_tokens": 1}},
    )
    monkeypatch.setattr(
        appmod,
        "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: b"sk-test-secret",
    )

    def fake_chat_completion(cfg, messages, **kwargs):
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        assert "Fresh start" in joined
        if "Extract Memory Garden" in joined:
            return {
                "reply": (
                    '{"memories":['
                    '{"type":"moment","title":"Fresh start","description":"User started without imported material.","occurred_at":"2026-06-01"},'
                    '{"type":"fact","title":"Blank setup","description":"No prior material was provided.","occurred_at":"2026-06-01"}'
                    "]}"
                ),
                "usage": {},
            }
        if "Derive a Feedling Identity Card" in joined:
            return {"reply": appmod.json.dumps(_identity_payload()), "usage": {}}
        return {"reply": "ok", "usage": {}}

    monkeypatch.setattr(appmod, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test-secret"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    upload = client.post(
        "/v1/history_import/upload",
        json={"format": "auto", "content": "", "fresh_start": True},
        headers=_headers(api_key),
    )
    assert upload.status_code == 201, upload.get_data(as_text=True)
    job = upload.get_json()["job"]
    assert job["status"] == "completed"
    assert job["messages_parsed"] == 0
    assert job["support_materials"] == 1
    assert "fresh_start_without_support_material" in job["warnings"]
    assert job["identity_written"] is True
