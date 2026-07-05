import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402

import provider_client  # noqa: E402
from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import auth as enclave_auth  # noqa: E402
from enclave import backend_client, envelope as envmod, keys  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave.routes import build_app  # noqa: E402

_FRAME_ID = "abc123def4560000"


@pytest.fixture
def enclave(monkeypatch):
    monkeypatch.setenv("FEEDLING_SCREEN_VLM_API_KEY", "sk-test")
    monkeypatch.setenv("FEEDLING_SCREEN_VLM_MODEL", "qwen/qwen3-vl-8b-instruct")
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    enclave_auth.reset_cache()

    async def fake_backend_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "u1"}
        assert path == f"/v1/screen/frames/{_FRAME_ID}/envelope"
        return {"v": 1, "ts": 1.0}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)

    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)

    monkeypatch.setattr(
        envmod, "decrypt_envelope",
        lambda env, uid, sk: json.dumps(
            {"image": "ZmFrZQ==", "ocr_text": "Inbox (3)", "app": "Mail"}
        ).encode(),
    )
    return _AsgiTestClient(build_app())


def test_caption_returns_text_never_pixels(enclave, monkeypatch):
    captured = {}

    async def fake_chat(config, messages, **kw):
        captured["messages"] = messages
        captured["model"] = config.model
        return {"reply": "Mail inbox with 3 unread threads."}

    monkeypatch.setattr(provider_client, "chat_completion_async", fake_chat)

    resp = enclave.get(f"/v1/screen/frames/{_FRAME_ID}/caption",
                       headers={"X-API-Key": "user-key"})
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
    resp = enclave.get(f"/v1/screen/frames/{_FRAME_ID}/caption",
                       headers={"X-API-Key": "user-key"})
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "screen_caption_unconfigured"


def test_caption_model_failure_is_fail_closed(enclave, monkeypatch):
    async def boom(config, messages, **kw):
        raise provider_client.ProviderError("upstream 429")

    monkeypatch.setattr(provider_client, "chat_completion_async", boom)
    resp = enclave.get(f"/v1/screen/frames/{_FRAME_ID}/caption",
                       headers={"X-API-Key": "user-key"})
    assert resp.status_code == 502
    assert resp.get_json()["error"].startswith("screen_caption_failed")
    assert "image_b64" not in resp.get_json()


def test_caption_full_mode_includes_ocr_not_pixels(enclave, monkeypatch):
    async def fake_chat(config, messages, **kw):
        return {"reply": "Mail app showing inbox with unread messages."}

    monkeypatch.setattr(provider_client, "chat_completion_async", fake_chat)

    resp = enclave.get(f"/v1/screen/frames/{_FRAME_ID}/caption?mode=full",
                       headers={"X-API-Key": "user-key"})
    body = resp.get_json()

    assert resp.status_code == 200
    # mode=full must include the OCR text field
    assert "ocr_text" in body
    assert "Inbox" in body["ocr_text"]
    # Privacy invariant still holds: no pixels in the response
    assert "image_b64" not in body and "image" not in body
