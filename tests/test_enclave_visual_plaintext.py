from __future__ import annotations

import base64
import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))


_FRAME_ID = "ab" * 8
_RAW_JPEG = b"\xff\xd8\xff\xe0raw-photo-pixels\xff\xd9"


@pytest.fixture
def enclave(monkeypatch):
    monkeypatch.setenv("FEEDLING_SCREEN_VLM_API_KEY", "sk-test")
    mod = importlib.import_module("enclave_app")
    monkeypatch.setitem(mod._state, "ready", True)
    monkeypatch.setitem(mod._state, "error", None)
    monkeypatch.setitem(mod.app.config, "TESTING", True)
    monkeypatch.setattr(mod, "_extract_api_key", lambda: "user-key")
    monkeypatch.setattr(mod, "_whoami_cached", lambda _key: {"user_id": "u1"})
    monkeypatch.setattr(
        mod,
        "_flask_get",
        lambda _path, _key: {"v": 1, "ts": 123.0, "id": _FRAME_ID},
    )
    monkeypatch.setattr(mod, "_get_or_derive_content_sk", lambda: object())
    return mod


def test_raw_photo_decrypt_returns_pixels(enclave, monkeypatch):
    monkeypatch.setattr(
        enclave, "_decrypt_envelope", lambda _env, _uid, _sk: _RAW_JPEG
    )

    response = enclave.app.test_client().get(
        f"/v1/screen/frames/{_FRAME_ID}/decrypt"
    )
    body = response.get_json()

    assert response.status_code == 200
    assert body["decrypt_status"] == "ok"
    assert body["image_b64"] == base64.b64encode(_RAW_JPEG).decode("ascii")
    assert body["image_mime"] == "image/jpeg"
    assert body["ocr_text"] == ""
    assert body["app"] is None


def test_raw_photo_caption_sends_pixels_only_to_vlm(enclave, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        enclave, "_decrypt_envelope", lambda _env, _uid, _sk: _RAW_JPEG
    )

    def fake_chat(_config, messages, **_kwargs):
        captured["messages"] = messages
        return {"reply": "A test photo."}

    monkeypatch.setattr(enclave.provider_client, "chat_completion", fake_chat)
    response = enclave.app.test_client().get(
        f"/v1/screen/frames/{_FRAME_ID}/caption"
    )
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
    monkeypatch.setattr(
        enclave, "_decrypt_envelope", lambda _env, _uid, _sk: _RAW_JPEG
    )

    response = enclave.app.test_client().get(
        f"/v1/screen/frames/{_FRAME_ID}/image"
    )

    assert response.status_code == 200
    assert response.content_type == "image/jpeg"
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
        enclave,
        "_decrypt_envelope",
        lambda _env, _uid, _sk: json.dumps(wrapped).encode("utf-8"),
    )

    response = enclave.app.test_client().get(
        f"/v1/screen/frames/{_FRAME_ID}/decrypt"
    )
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
        enclave,
        "_decrypt_envelope",
        lambda _env, _uid, _sk: b"not-json-and-not-an-image",
    )

    response = enclave.app.test_client().get(
        f"/v1/screen/frames/{_FRAME_ID}/decrypt"
    )

    assert response.status_code == 502
    assert response.get_json()["error"].startswith("plaintext_parse:")
