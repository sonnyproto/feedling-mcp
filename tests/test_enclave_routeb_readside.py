from __future__ import annotations

import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import enclave_app  # noqa: E402


def _moment(mid: str, title: str, description: str, *, linked: str = "") -> dict:
    return {
        "id": mid,
        "title": title,
        "description": description,
        "type": "fact",
        "source": "test",
        "occurred_at": "2026-06-21T10:00:00",
        "created_at": "2026-06-21T10:00:00",
        "her_quote": "",
        "context": "",
        "linked_dimension": linked,
    }


def test_routeb_readside_helper_selects_index_then_returns_old_context_shape():
    moments = [
        _moment(
            "mem_cat",
            "猫咪照顾",
            "用户聊猫咪健康问题时，先需要被安抚，再给观察饮水和精神状态的建议。",
            linked="猫咪",
        ),
        _moment(
            "mem_lark",
            "Lark 工作流",
            "用户希望 agent 帮忙读 Lark 群消息并整理重点。",
            linked="Lark",
        ),
    ]

    selected, trace = enclave_app._select_context_memories_via_readside(
        moments,
        "猫咪最近不吃饭，我有点担心",
    )

    assert [item["id"] for item in selected] == ["mem_cat"]
    assert selected[0]["title"] == "猫咪照顾"
    assert "summary" not in selected[0]
    assert trace["mode"] == "model_api_readside_v1"
    assert trace["readside_enabled"] is True
    assert trace["index_count"] == 2
    assert trace["selected"][0]["id"] == "mem_cat"


@pytest.fixture()
def enclave_history_client(monkeypatch):
    previous_ready = enclave_app._state.get("ready")
    previous_error = enclave_app._state.get("error")
    enclave_app._state["ready"] = True
    enclave_app._state["error"] = None
    monkeypatch.setattr(enclave_app, "_extract_api_key", lambda: "key_routeb")
    monkeypatch.setattr(enclave_app, "_whoami_cached", lambda _key: {"user_id": "usr_routeb"})
    monkeypatch.setattr(enclave_app, "_get_or_derive_content_sk", lambda: object())

    def fake_flask_get(path, api_key, params=None):
        assert api_key == "key_routeb"
        assert path == "/v1/chat/history"
        return {
            "messages": [
                {
                    "id": "chat_1",
                    "role": "user",
                    "ts": 1,
                    "v": 1,
                    "visibility": "shared",
                    "content_type": "text",
                }
            ],
            "total": 1,
        }

    monkeypatch.setattr(enclave_app, "_flask_get", fake_flask_get)
    monkeypatch.setattr(enclave_app, "_decrypt_envelope", lambda _m, _uid, _sk: "猫咪最近不吃饭".encode("utf-8"))
    monkeypatch.setattr(
        enclave_app,
        "_load_decrypted_moments",
        lambda _api_key, _uid, _sk, limit=200: [
            _moment("mem_cat", "猫咪照顾", "用户聊猫咪健康问题时，先需要被安抚，再给观察饮水和精神状态的建议。", linked="猫咪"),
            _moment("mem_lark", "Lark 工作流", "用户希望 agent 帮忙读 Lark 群消息并整理重点。", linked="Lark"),
        ],
    )
    enclave_app.app.config.update(TESTING=True)
    with enclave_app.app.test_client() as client:
        yield client
    enclave_app._state["ready"] = previous_ready
    enclave_app._state["error"] = previous_error


def test_routeb_flag_false_keeps_legacy_context_selection(enclave_history_client, monkeypatch):
    monkeypatch.delenv("MEMORY_READSIDE_FOR_MODEL_API", raising=False)
    monkeypatch.setattr(
        enclave_app,
        "select_context_memories_with_trace",
        lambda moments, latest, mode="": ([{"id": "legacy", "title": "legacy"}], {"mode": "model_api"}),
    )
    monkeypatch.setattr(
        enclave_app,
        "_select_context_memories_via_readside",
        lambda *_args, **_kwargs: pytest.fail("readside should not run when flag is false"),
    )

    res = enclave_history_client.get("/v1/chat/history?context_mode=model_api&context_trace=1")

    assert res.status_code == 200
    body = res.get_json()
    assert body["context_memories"] == [{"id": "legacy", "title": "legacy"}]
    assert body["context_memory_trace"]["mode"] == "model_api"


def test_routeb_flag_true_uses_readside_selection(enclave_history_client, monkeypatch):
    monkeypatch.setenv("MEMORY_READSIDE_FOR_MODEL_API", "true")
    monkeypatch.setattr(
        enclave_app,
        "select_context_memories_with_trace",
        lambda *_args, **_kwargs: pytest.fail("legacy selector should not run when readside flag is true"),
    )

    res = enclave_history_client.get("/v1/chat/history?context_mode=model_api&context_trace=1")

    assert res.status_code == 200
    body = res.get_json()
    assert [item["id"] for item in body["context_memories"]] == ["mem_cat"]
    assert body["context_memory_trace"]["mode"] == "model_api_readside_v1"
    assert body["context_memory_trace"]["readside_enabled"] is True


def test_routeb_readside_uses_configurable_memory_limit(enclave_history_client, monkeypatch):
    captured_limits = []

    def fake_load(api_key, uid, sk, limit=200):
        captured_limits.append(limit)
        return [
            _moment("mem_cat", "猫咪照顾", "用户聊猫咪健康问题时，先需要被安抚。", linked="猫咪"),
        ]

    monkeypatch.setattr(enclave_app, "_load_decrypted_moments", fake_load)

    monkeypatch.delenv("MEMORY_READSIDE_FOR_MODEL_API", raising=False)
    enclave_history_client.get("/v1/chat/history?context_mode=model_api&context_trace=1")
    assert captured_limits[-1] == 200

    monkeypatch.setenv("MEMORY_READSIDE_FOR_MODEL_API", "true")
    monkeypatch.delenv("MEMORY_READSIDE_MODEL_API_LIMIT", raising=False)
    enclave_history_client.get("/v1/chat/history?context_mode=model_api&context_trace=1")
    assert captured_limits[-1] == 50

    monkeypatch.setenv("MEMORY_READSIDE_MODEL_API_LIMIT", "80")
    enclave_history_client.get("/v1/chat/history?context_mode=model_api&context_trace=1")
    assert captured_limits[-1] == 80

    monkeypatch.setenv("MEMORY_READSIDE_MODEL_API_LIMIT", "999")
    enclave_history_client.get("/v1/chat/history?context_mode=model_api&context_trace=1")
    assert captured_limits[-1] == 200
