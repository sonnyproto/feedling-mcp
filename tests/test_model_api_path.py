from __future__ import annotations

import base64
import os
import sys
import threading
import time
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import app as appmod  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "FEEDLING_DIR", tmp_path)
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


def _wait_history_import_job(client, api_key: str, job_id: str, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    last_job = {}
    while time.time() < deadline:
        res = client.get(f"/v1/history_import/status/{job_id}", headers=_headers(api_key))
        assert res.status_code == 200, res.get_data(as_text=True)
        last_job = res.get_json()["job"]
        if last_job["status"] in {"completed", "failed"}:
            return last_job
        time.sleep(0.02)
    raise AssertionError(f"history import job did not finish: {last_job}")


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

    config_text = appmod.json.dumps(appmod.db.get_blob(user_id, "model_api") or {})
    assert raw_provider_key not in config_text
    assert "api_key_envelope" in config_text

    validate = client.get("/v1/onboarding/validate", headers=_headers(api_key))
    assert validate.status_code == 200
    body = validate.get_json()
    assert body["route"] == "model_api"
    assert body["stage"] == "history_import"
    assert all(step["id"] != "resident_consumer" for step in body["steps"])


def test_model_api_setup_logs_provider_test_failure(client, monkeypatch, capsys):
    # A failed self-test (bad/quota'd key, or an unsupported model name) must
    # leave a server-side log line with provider/model/status_code so the
    # failure is traceable — the response body alone never reaches the logs.
    _, api_key = _register(client)

    def boom(cfg):
        raise appmod.ProviderError(
            "provider_http_404: model: claude-3-5-haiku-latest", status_code=404
        )

    monkeypatch.setattr(appmod, "test_provider_key", boom)

    setup = client.post(
        "/v1/model_api/setup",
        json={
            "provider": "anthropic",
            "model": "claude-3-5-haiku-latest",
            "api_key": "sk-ant-whatever",
        },
        headers=_headers(api_key),
    )
    assert setup.status_code == 400
    assert setup.get_json()["error"] == "provider_test_failed"

    out = capsys.readouterr().out
    assert "anthropic" in out
    assert "claude-3-5-haiku-latest" in out
    assert "404" in out
    assert "sk-ant-whatever" not in out  # never log the raw provider key


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
            "client_job_id": "test-history-import-complete",
        },
        headers=_headers(api_key),
    )
    assert upload.status_code == 202, upload.get_data(as_text=True)
    queued_job = upload.get_json()["job"]
    assert queued_job["status"] == "queued"
    assert queued_job["phase"] == "upload_received"
    duplicate = client.post(
        "/v1/history_import/upload",
        json={
            "format": "plaintext",
            "content": transcript,
            "relationship_started_at": "2026-05-31",
            "client_job_id": "test-history-import-complete",
        },
        headers=_headers(api_key),
    )
    assert duplicate.status_code in (200, 202), duplicate.get_data(as_text=True)
    assert duplicate.get_json()["job"]["job_id"] == queued_job["job_id"]
    job = _wait_history_import_job(client, api_key, queued_job["job_id"])
    assert job["status"] == "completed"
    assert job["phase"] == "completed"
    assert job["progress"] == 100
    assert job["messages_parsed"] == 2
    assert job["memories_created"] >= 2
    assert job["identity_written"] is True
    assert job["chat_messages_imported"] == 0
    assert job["onboarding_greeting_written"] is True

    mid_validate = client.get("/v1/onboarding/validate", headers=_headers(api_key)).get_json()
    assert mid_validate["route"] == "model_api"
    assert mid_validate["stage"] == "complete"
    assert mid_validate["passing"] is True

    pre_chat = client.get("/v1/chat/history?limit=20", headers=_headers(api_key))
    assert pre_chat.status_code == 200
    pre_rows = pre_chat.get_json()["messages"]
    assert len(pre_rows) == 1
    assert not any(row["source"] == "history_import" for row in pre_rows)
    assert pre_rows[0]["source"] == "model_api"
    assert pre_rows[0]["role"] == "openclaw"
    assert pre_rows[0]["model_api_kind"] == "onboarding_greeting"

    def fake_enclave_context(path, key, params=None):
        if path == "/v1/chat/history":
            return {
                "messages": [
                    {
                        "role": "openclaw",
                        "content": "I can answer from the imported history now.",
                        "source": "model_api",
                    },
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
    assert not any(row["source"] == "history_import" for row in rows)
    assert any(
        row["source"] == "model_api"
        and row["role"] == "openclaw"
        and row.get("model_api_kind") == "onboarding_greeting"
        for row in rows
    )
    assert any(row["source"] == "model_api" and row["role"] == "user" for row in rows)
    assert any(row["source"] == "model_api" and row["role"] == "openclaw" for row in rows)
    assert all("body_ct" in row for row in rows if row["source"] == "model_api")
    assert "sk-test-secret" not in appmod.json.dumps(appmod.db.get_blob(user_id, "model_api") or {})


def test_history_import_reuses_inflight_client_job(client, monkeypatch):
    user_id, api_key = _register(client)
    release_provider = threading.Event()
    provider_entered = threading.Event()

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
            provider_entered.set()
            assert release_provider.wait(timeout=2)
            return {
                "reply": (
                    '{"memories":['
                    '{"type":"moment","title":"Inflight moment","description":"The job was reused.","occurred_at":"2026-05-31"},'
                    '{"type":"fact","title":"Inflight fact","description":"Duplicate start did not duplicate work.","occurred_at":"2026-05-31"}'
                    "]}"
                ),
                "usage": {},
            }
        if "Derive a Feedling Identity Card" in joined:
            return {"reply": appmod.json.dumps(_identity_payload()), "usage": {}}
        return {"reply": "Ready.", "usage": {}}

    monkeypatch.setattr(appmod, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test-secret"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    payload = {
        "format": "plaintext",
        "content": "2026-05-31 User: Please reuse this import job.",
        "relationship_started_at": "2026-05-31",
        "client_job_id": "test-inflight-reuse",
    }
    first = client.post("/v1/history_import/upload", json=payload, headers=_headers(api_key))
    assert first.status_code == 202, first.get_data(as_text=True)
    first_job = first.get_json()["job"]
    assert provider_entered.wait(timeout=2)

    duplicate = client.post("/v1/history_import/upload", json=payload, headers=_headers(api_key))
    assert duplicate.status_code == 202, duplicate.get_data(as_text=True)
    assert duplicate.get_json()["job"]["job_id"] == first_job["job_id"]

    release_provider.set()
    job = _wait_history_import_job(client, api_key, first_job["job_id"])
    assert job["status"] == "completed"
    assert job["memories_created"] >= 2


def test_model_api_chat_send_accepts_user_image(client, monkeypatch):
    _, api_key = _register(client)
    captured = {}

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
        if any(isinstance(m.get("content"), list) for m in messages):
            captured["messages"] = messages
        return {"reply": "I can see the image.", "usage": {"total_tokens": 11}}

    monkeypatch.setattr(appmod, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test-secret"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    chat = client.post(
        "/v1/model_api/chat/send",
        json={
            "message": "What is in this image?",
            "image_mime": "image/jpeg",
            "image_b64": _b64(b"fake-jpeg-bytes"),
        },
        headers=_headers(api_key),
    )
    assert chat.status_code == 200, chat.get_data(as_text=True)
    assert chat.get_json()["user_content_type"] == "image"

    user_messages = [m for m in captured["messages"] if m.get("role") == "user"]
    content = user_messages[-1]["content"]
    assert content[0] == {"type": "text", "text": "What is in this image?"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    history = client.get("/v1/chat/history?limit=10", headers=_headers(api_key))
    rows = history.get_json()["messages"]
    assert any(row["role"] == "user" and row["content_type"] == "image" for row in rows)


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
            "client_job_id": "test-json-history-import",
        },
        headers=_headers(api_key),
    )
    assert upload.status_code == 202, upload.get_data(as_text=True)
    job = _wait_history_import_job(client, api_key, upload.get_json()["job"]["job_id"])
    assert job["status"] == "completed"
    assert job["messages_parsed"] == 2
    assert job["support_materials"] == 1
    assert job["history_filename"] == "chat-export.json"
    assert job["persona_filename"] == "persona.md"
    assert job["memories_created"] >= 2
    assert job["identity_written"] is True


def test_wrapped_chat_history_json_parses_without_upload_artifacts():
    chat_export = [
        {
            "mapping": {
                "u1": {
                    "message": {
                        "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": ["我最近在测试 API onboarding。"]},
                        "create_time": 1780200000.0,
                    }
                },
                "a1": {
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"content_type": "text", "parts": ["我会把导入内容变成可读记忆。"]},
                        "create_time": 1780200060.0,
                    }
                },
                "sys": {
                    "message": {
                        "author": {"role": "system"},
                        "content": {"content_type": "text", "parts": ["internal setup"]},
                        "create_time": 1780200120.0,
                    }
                },
            }
        }
    ]
    wrapped = (
        "===== BEGIN CHAT HISTORY FILE: conversations-011.json =====\n"
        + appmod.json.dumps(chat_export, ensure_ascii=False)
        + "\n===== END CHAT HISTORY FILE: conversations-011.json ====="
    )
    warnings = []

    messages = appmod._parse_import_history_content(wrapped, "auto", warnings)

    assert warnings == []
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert "API onboarding" in messages[0]["content"]
    assert all("BEGIN CHAT HISTORY FILE" not in m["content"] for m in messages)
    assert all("conversation_id" not in m["content"] for m in messages)

    cards = appmod._fallback_memory_cards(
        messages,
        appmod.date(2026, 5, 31),
        story_needed=1,
        about_needed=1,
        language=appmod._detect_import_language(messages),
    )
    assert len(cards) == 2
    assert cards[0]["title"].startswith("导入")
    assert all("BEGIN CHAT HISTORY FILE" not in card["description"] for card in cards)


def test_support_material_sections_split_character_and_personal_profile():
    payload = {
        "persona_filename": "combined.md",
        "persona_content": """
===== BEGIN CHARACTER CARD =====
小哆啦是一个稳定、细心、会记得小事的陪伴型 AI。
===== END CHARACTER CARD =====

===== BEGIN PERSONAL PROFILE CARD: profile.md =====
用户喜欢直接的反馈，也希望记忆写得像人能读懂的话。
===== END PERSONAL PROFILE CARD: profile.md =====
""",
    }

    support = appmod._persona_support_messages(payload)

    assert [m["source"] for m in support] == ["character_import", "persona_import"]
    assert "小哆啦" in support[0]["content"]
    assert "用户喜欢直接的反馈" in support[1]["content"]
    assert all("BEGIN " not in m["content"] and "END " not in m["content"] for m in support)


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
        json={
            "format": "auto",
            "content": "",
            "fresh_start": True,
            "client_job_id": "test-fresh-start-import",
        },
        headers=_headers(api_key),
    )
    assert upload.status_code == 202, upload.get_data(as_text=True)
    job = _wait_history_import_job(client, api_key, upload.get_json()["job"]["job_id"])
    assert job["status"] == "completed"
    assert job["messages_parsed"] == 0
    assert job["support_materials"] == 1
    assert "fresh_start_without_support_material" in job["warnings"]
    assert job["identity_written"] is True
