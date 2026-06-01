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


def _plain_identity() -> dict:
    return {
        "agent_name": "bro",
        "self_introduction": "I keep the real thread with you.",
        "category": "Blunt · Observant",
        "signature": ["Direct", "Context-heavy"],
        "dimensions": [
            {"name": "Signal sensitivity", "value": 92, "description": "Notices subtle shifts."},
            {"name": "Context retention", "value": 88, "description": "Keeps prior context."},
            {"name": "Narrative loyalty", "value": 82, "description": "Tracks the real thread."},
            {"name": "Strategic sharpness", "value": 74, "description": "Finds the useful next move."},
            {"name": "Evidence discipline", "value": 70, "description": "Asks for receipts."},
            {"name": "Aesthetic weirdness", "value": 76, "description": "Keeps a distinctive voice."},
            {"name": "Tenderness under pressure", "value": 58, "description": "Can soften under stress."},
        ],
    }


def _seed_identity(user_id: str) -> None:
    store = appmod.get_store(user_id)
    store.identity_file.write_text(json.dumps({
        "v": 1,
        "id": "identity_1",
        "body_ct": "old",
        "nonce": "old_nonce",
        "K_user": "old_k_user",
        "K_enclave": "old_k_enclave",
        "visibility": "shared",
        "owner_user_id": user_id,
        "created_at": "2026-05-31T00:00:00",
        "updated_at": "2026-05-31T00:00:00",
        "relationship_started_at": "2026-04-01",
        "relationship_anchor_source": "test",
        "relationship_anchor_evidence": "seeded identity for test",
    }))


def _seed_memory(user_id: str, memory_id: str = "mom_1") -> None:
    store = appmod.get_store(user_id)
    store.memory_file.write_text(json.dumps([{
        "v": 1,
        "id": memory_id,
        "type": "fact",
        "occurred_at": "2026-05-01",
        "created_at": "2026-05-01T00:00:00",
        "source": "bootstrap",
        "body_ct": "old",
        "nonce": "old_nonce",
        "K_user": "old_k_user",
        "K_enclave": "old_k_enclave",
        "visibility": "shared",
        "owner_user_id": user_id,
    }]))


def _plain_memory() -> dict:
    return {
        "title": "Wrong city",
        "description": "User moved to New York in May.",
        "type": "fact",
        "context": "Imported profile",
    }


def _fake_envelope_builder(captured: list):
    def _build(store, plaintext: bytes, item_id: str | None = None):
        try:
            captured.append(json.loads(plaintext.decode("utf-8")))
        except Exception:
            captured.append(plaintext.decode("utf-8"))
        return {
            "id": item_id or "env_1",
            "body_ct": f"ct_{len(captured)}",
            "nonce": f"nonce_{len(captured)}",
            "K_user": f"k_user_{len(captured)}",
            "K_enclave": f"k_enclave_{len(captured)}",
            "visibility": "shared",
            "owner_user_id": store.user_id,
            "enclave_pk_fpr": "test",
        }, ""
    return _build


def test_identity_profile_patch_reencrypts_existing_card(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_identity(user_id)
    captured_plaintexts: list = []

    monkeypatch.setattr(
        appmod,
        "_enclave_get_json_for_gate",
        lambda path, key, params=None: ({"identity": _plain_identity()}, "") if path == "/v1/identity/get" else ({}, ""),
    )
    monkeypatch.setattr(appmod, "_build_shared_envelope_for_store", _fake_envelope_builder(captured_plaintexts))

    res = client.post(
        "/v1/identity/actions",
        headers=_headers(api_key),
        json={
            "actions": [{
                "type": "identity.profile_patch",
                "patch": {"agent_name": "小秘"},
                "reason": "User asked for a displayed name change.",
            }],
        },
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["status"] == "ok"
    assert body["effects"][0]["type"] == "identity_updated"
    assert body["effects"][0]["fields"] == ["agent_name"]
    assert captured_plaintexts[-1]["agent_name"] == "小秘"
    assert captured_plaintexts[-1]["self_introduction"] == _plain_identity()["self_introduction"]
    saved = json.loads(appmod.get_store(user_id).identity_file.read_text())
    assert saved["id"] == "identity_1"
    assert saved["body_ct"] == "ct_1"
    assert saved["relationship_started_at"] == "2026-04-01"


def test_model_api_chat_executes_detected_identity_rename(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_identity(user_id)
    captured_plaintexts: list = []

    monkeypatch.setattr(
        appmod,
        "test_provider_key",
        lambda cfg: {"reply": "ok", "usage": {"total_tokens": 1}},
    )
    monkeypatch.setattr(appmod, "_decrypt_envelope_via_enclave", lambda envelope, key, purpose: b"sk-test")
    monkeypatch.setattr(appmod, "_build_shared_envelope_for_store", _fake_envelope_builder(captured_plaintexts))

    def fake_enclave_context(path, key, params=None):
        if path == "/v1/identity/get":
            return {"identity": _plain_identity()}, ""
        if path == "/v1/chat/history":
            return {"messages": [], "context_memories": []}, ""
        return {}, ""

    monkeypatch.setattr(appmod, "_enclave_get_json_for_gate", fake_enclave_context)
    monkeypatch.setattr(
        appmod,
        "chat_completion",
        lambda cfg, messages, **kwargs: {"reply": "改好了，我现在叫小秘。", "usage": {"total_tokens": 9}},
    )

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    res = client.post(
        "/v1/model_api/chat/send",
        json={"message": "call yourself 小秘"},
        headers=_headers(api_key),
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["reply"] == "改好了，我现在叫小秘。"
    assert body["effects"][0]["action"] == "identity.profile_patch"
    assert body["identity_actions"][0]["changed_fields"] == ["agent_name"]
    assert any(isinstance(item, dict) and item.get("agent_name") == "小秘" for item in captured_plaintexts)


def test_memory_content_patch_reencrypts_existing_card(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_memory(user_id)
    captured_plaintexts: list = []

    monkeypatch.setattr(
        appmod,
        "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: json.dumps(_plain_memory()).encode("utf-8"),
    )
    monkeypatch.setattr(appmod, "_build_shared_envelope_for_store", _fake_envelope_builder(captured_plaintexts))

    res = client.post(
        "/v1/memory/actions",
        headers=_headers(api_key),
        json={
            "actions": [{
                "type": "memory.content_patch",
                "memory_id": "mom_1",
                "patch": {"description": "User moved to Tokyo in April."},
                "reason": "User corrected this card.",
            }],
        },
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["status"] == "ok"
    assert body["effects"][0]["type"] == "memory_updated"
    assert body["effects"][0]["memory_id"] == "mom_1"
    assert captured_plaintexts[-1]["description"] == "User moved to Tokyo in April."
    assert captured_plaintexts[-1]["title"] == "Wrong city"
    saved = json.loads(appmod.get_store(user_id).memory_file.read_text())[0]
    assert saved["id"] == "mom_1"
    assert saved["body_ct"] == "ct_1"
    assert saved["updated_at"]


def test_model_api_chat_executes_memory_context_patch(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_identity(user_id)
    _seed_memory(user_id)
    captured_plaintexts: list = []

    monkeypatch.setattr(appmod, "test_provider_key", lambda cfg: {"reply": "ok", "usage": {}})

    def fake_decrypt(envelope, key, purpose):
        if purpose == "model_api_provider_key":
            return b"sk-test"
        return json.dumps(_plain_memory()).encode("utf-8")

    monkeypatch.setattr(appmod, "_decrypt_envelope_via_enclave", fake_decrypt)
    monkeypatch.setattr(appmod, "_build_shared_envelope_for_store", _fake_envelope_builder(captured_plaintexts))

    def fake_enclave_context(path, key, params=None):
        if path == "/v1/identity/get":
            return {"identity": _plain_identity()}, ""
        if path == "/v1/chat/history":
            return {"messages": [], "context_memories": []}, ""
        return {}, ""

    calls = {"n": 0}

    def fake_chat_completion(cfg, messages, **kwargs):
        calls["n"] += 1
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        if "Memory Capture worker" in joined:
            return {"reply": '{"memories":[]}', "usage": {}}
        return {"reply": "改好了，Memory Garden 已更新。", "usage": {}}

    monkeypatch.setattr(appmod, "_enclave_get_json_for_gate", fake_enclave_context)
    monkeypatch.setattr(appmod, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    res = client.post(
        "/v1/model_api/chat/send",
        json={
            "message": "这张记忆写错了，改成 User moved to Tokyo in April.",
            "context_refs": [{"type": "memory", "id": "mom_1", "title": "Wrong city"}],
        },
        headers=_headers(api_key),
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["effects"][0]["action"] == "memory.content_patch"
    assert body["memory_actions"][0]["changed_fields"] == ["description"]
    assert any(isinstance(item, dict) and item.get("description") == "User moved to Tokyo in April." for item in captured_plaintexts)
    assert body["context"]["context_refs"] == 1
