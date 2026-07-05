from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import auth as enclave_auth  # noqa: E402
from enclave import backend_client, envelope as envmod, keys  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave.routes import build_app  # noqa: E402


_FRAME_ID = "ab" * 8
_RAW_JPEG = b"\xff\xd8\xff\xe0raw-photo-pixels\xff\xd9"


@pytest.fixture
def enclave(monkeypatch):
    monkeypatch.setenv("FEEDLING_SCREEN_VLM_API_KEY", "sk-test")
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    enclave_auth.reset_cache()

    async def fake_backend_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "u1"}
        assert path == f"/v1/screen/frames/{_FRAME_ID}/envelope"
        return {"v": 1, "ts": 123.0, "id": _FRAME_ID}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)

    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)

    return _AsgiTestClient(build_app())


def test_raw_photo_decrypt_returns_pixels(enclave, monkeypatch):
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: _RAW_JPEG)

    response = enclave.get(f"/v1/screen/frames/{_FRAME_ID}/decrypt",
                           headers={"X-API-Key": "user-key"})
    body = response.get_json()

    assert response.status_code == 200
    assert body["decrypt_status"] == "ok"
    assert body["image_b64"] == base64.b64encode(_RAW_JPEG).decode("ascii")
    assert body["image_mime"] == "image/jpeg"
    assert body["ocr_text"] == ""
    assert body["app"] is None


def test_raw_photo_caption_sends_pixels_only_to_vlm(enclave, monkeypatch):
    captured = {}
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: _RAW_JPEG)

    import provider_client

    async def fake_chat(_config, messages, **_kwargs):
        captured["messages"] = messages
        return {"reply": "A test photo."}

    monkeypatch.setattr(provider_client, "chat_completion_async", fake_chat)
    response = enclave.get(f"/v1/screen/frames/{_FRAME_ID}/caption",
                           headers={"X-API-Key": "user-key"})
    body = response.get_json()

    assert response.status_code == 200
    assert body["caption"] == "A test photo."
    assert "image_b64" not in body and "image" not in body
    expected_url = (
        "data:image/jpeg;base64,"
        + base64.b64encode(_RAW_JPEG).decode("ascii")
    )
    assert expected_url in json.dumps(captured["messages"])


def test_raw_photo_image_returns_original_bytes(enclave, monkeypatch):
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: _RAW_JPEG)

    response = enclave.get(f"/v1/screen/frames/{_FRAME_ID}/image",
                           headers={"X-API-Key": "user-key"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.data == _RAW_JPEG


def test_wrapped_screen_frame_decrypt_is_unchanged(enclave, monkeypatch):
    wrapped = {
        "image": base64.b64encode(b"screen-jpeg").decode("ascii"),
        "ocr_text": "Inbox (3)",
        "app": "Mail",
        "bundle": "com.apple.mobilemail",
        "w": 1179,
        "h": 2556,
    }
    monkeypatch.setattr(
        envmod, "decrypt_envelope",
        lambda e, u, s: json.dumps(wrapped).encode("utf-8"),
    )

    response = enclave.get(f"/v1/screen/frames/{_FRAME_ID}/decrypt",
                           headers={"X-API-Key": "user-key"})
    body = response.get_json()

    assert response.status_code == 200
    assert body["decrypt_status"] == "ok"
    assert body["image_b64"] == wrapped["image"]
    assert body["image_mime"] == "image/jpeg"
    assert body["ocr_text"] == "Inbox (3)"
    assert body["app"] == "Mail"
    assert body["bundle"] == "com.apple.mobilemail"


def test_malformed_non_image_plaintext_still_fails_closed(enclave, monkeypatch):
    monkeypatch.setattr(
        envmod, "decrypt_envelope",
        lambda e, u, s: b"not-json-and-not-an-image",
    )

    response = enclave.get(f"/v1/screen/frames/{_FRAME_ID}/decrypt",
                           headers={"X-API-Key": "user-key"})

    assert response.status_code == 502
    assert response.get_json()["error"].startswith("plaintext_parse:")
