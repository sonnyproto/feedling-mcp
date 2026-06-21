import json
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest


@pytest.fixture
def enclave(monkeypatch):
    monkeypatch.setenv("FEEDLING_SCREEN_VLM_API_KEY", "sk-test")
    monkeypatch.setenv("FEEDLING_SCREEN_VLM_MODEL", "qwen/qwen3-vl-8b-instruct")
    mod = importlib.import_module("enclave_app")
    mod._state["ready"] = True
    monkeypatch.setattr(mod, "_extract_api_key", lambda: "user-key")
    monkeypatch.setattr(mod, "_whoami_cached", lambda k: {"user_id": "u1"})
    monkeypatch.setattr(mod, "_flask_get", lambda path, key: {"v": 1, "ts": 1.0})
    monkeypatch.setattr(mod, "_get_or_derive_content_sk", lambda: object())
    monkeypatch.setattr(
        mod, "_decrypt_envelope",
        lambda env, uid, sk: json.dumps(
            {"image": "ZmFrZQ==", "ocr_text": "Inbox (3)", "app": "Mail"}
        ).encode(),
    )
    return mod


def test_caption_returns_text_never_pixels(enclave, monkeypatch):
    captured = {}

    def fake_chat(config, messages, **kw):
        captured["messages"] = messages
        captured["model"] = config.model
        return {"reply": "Mail inbox with 3 unread threads."}

    monkeypatch.setattr(enclave.provider_client, "chat_completion", fake_chat)
    client = enclave.app.test_client()

    resp = client.get("/v1/screen/frames/abc123def4560000/caption")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["caption"] == "Mail inbox with 3 unread threads."
    assert body["model"] == "qwen/qwen3-vl-8b-instruct"
    # Privacy invariant: pixels never leave the enclave to the backend caller.
    assert "image_b64" not in body and "image" not in body
    # The frame image WAS sent to the VLM as a data URL.
    blob = json.dumps(captured["messages"])
    assert "data:image/jpeg;base64,ZmFrZQ==" in blob


def test_caption_unconfigured_is_fail_closed(enclave, monkeypatch):
    monkeypatch.delenv("FEEDLING_SCREEN_VLM_API_KEY", raising=False)
    resp = enclave.app.test_client().get("/v1/screen/frames/abc123def4560000/caption")
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "screen_caption_unconfigured"


def test_caption_model_failure_is_fail_closed(enclave, monkeypatch):
    def boom(config, messages, **kw):
        raise enclave.provider_client.ProviderError("upstream 429")

    monkeypatch.setattr(enclave.provider_client, "chat_completion", boom)
    resp = enclave.app.test_client().get("/v1/screen/frames/abc123def4560000/caption")
    assert resp.status_code == 502
    assert resp.get_json()["error"].startswith("screen_caption_failed")
    assert "image_b64" not in resp.get_json()


def test_caption_full_mode_includes_ocr_not_pixels(enclave, monkeypatch):
    def fake_chat(config, messages, **kw):
        return {"reply": "Mail app showing inbox with unread messages."}

    monkeypatch.setattr(enclave.provider_client, "chat_completion", fake_chat)
    client = enclave.app.test_client()

    resp = client.get("/v1/screen/frames/abc123def4560000/caption?mode=full")
    body = resp.get_json()

    assert resp.status_code == 200
    # mode=full must include the OCR text field
    assert "ocr_text" in body
    assert "Inbox" in body["ocr_text"]
    # Privacy invariant still holds: no pixels in the response
    assert "image_b64" not in body and "image" not in body
