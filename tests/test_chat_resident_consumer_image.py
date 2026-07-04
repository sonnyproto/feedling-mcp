"""
Image turn pipeline tests for tools/chat_resident_consumer.py
=============================================================

Covers:
  - payload decode from image_b64
  - file write to IMAGE_TEMP_DIR
  - CLI template injection of image paths

Run with:
    cd backend && PYTHONPATH=. /path/to/venv/python -m pytest \
        ../tests/test_chat_resident_consumer_image.py -v
"""

import base64
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Module bootstrap — set required env vars BEFORE importing consumer.
# consumer reads env at module scope; these must exist first.
# ---------------------------------------------------------------------------

_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_image_checkpoint.json",
}

for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

# Ensure repo root + backend on path (mirrors existing test suite).
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

# Stub content_encryption when backend tree is absent.
try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules.setdefault("content_encryption", _fake_enc)

import tools.chat_resident_consumer as crc  # noqa: E402  (after env setup)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_JPEG_MAGIC = b"\xff\xd8\xff\xe0" + b"\x00" * 12  # minimal fake JPEG header
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8   # minimal fake PNG header


def _make_image_msg(
    ts: float = 1.0,
    image_bytes: bytes = _JPEG_MAGIC,
    mime: str = "image/jpeg",
    role: str = "user",
    msg_id: str = "test-img-01",
) -> dict:
    return {
        "id": msg_id,
        "role": role,
        "content": "",
        "content_type": "image",
        "ts": ts,
        "image_b64": base64.b64encode(image_bytes).decode("ascii"),
        "image_mime": mime,
    }


# ---------------------------------------------------------------------------
# Test 1: payload decode from image_b64
# ---------------------------------------------------------------------------

def test_image_payloads_decoded_from_msg():
    """_image_payloads_from_msg returns at least one payload with mime + data."""
    msg = _make_image_msg(image_bytes=_JPEG_MAGIC, mime="image/jpeg")
    payloads = crc._image_payloads_from_msg(msg)

    assert payloads, "expected at least one payload for an image message"
    p = payloads[0]
    assert p.get("mime_type") == "image/jpeg", f"unexpected mime_type: {p}"
    assert p.get("data"), "payload data (base64) must be non-empty"
    assert p.get("data_url", "").startswith("data:image/jpeg;base64,"), (
        f"data_url malformed: {p.get('data_url', '')[:80]}"
    )

    # Round-trip: decoded bytes must equal the original image bytes.
    decoded = base64.b64decode(p["data"])
    assert decoded == _JPEG_MAGIC, "round-trip bytes mismatch"


def test_image_payloads_empty_when_no_image_b64():
    """_image_payloads_from_msg returns [] when image_b64 is absent."""
    msg = {"role": "user", "content": "no image here", "ts": 2.0}
    payloads = crc._image_payloads_from_msg(msg)
    assert payloads == []


def test_image_payloads_handles_data_url_prefix():
    """image_b64 sent as a data-URL (data:image/png;base64,...) is stripped cleanly."""
    raw = _PNG_MAGIC
    b64 = base64.b64encode(raw).decode("ascii")
    data_url = f"data:image/png;base64,{b64}"
    msg = {
        "role": "user",
        "content": "",
        "content_type": "image",
        "ts": 3.0,
        "image_b64": data_url,
        "image_mime": "image/png",
    }
    payloads = crc._image_payloads_from_msg(msg)
    assert payloads, "expected payload from data-URL-prefixed image_b64"
    assert payloads[0]["mime_type"] == "image/png"
    assert base64.b64decode(payloads[0]["data"]) == raw


# ---------------------------------------------------------------------------
# Test 2: file write to IMAGE_TEMP_DIR
# ---------------------------------------------------------------------------

def test_image_file_paths_written(tmp_path):
    """_image_file_paths_for_msg writes image bytes to IMAGE_TEMP_DIR.
    The returned paths must exist and contain the original image bytes.
    """
    msg = _make_image_msg(image_bytes=_JPEG_MAGIC, mime="image/jpeg", msg_id="img-write-01")

    with patch.object(crc, "IMAGE_TEMP_DIR", tmp_path):
        paths = crc._image_file_paths_for_msg(msg)

    assert paths, "expected at least one file path to be returned"
    for p in paths:
        path = Path(p)
        assert path.exists(), f"image file not written: {p}"
        assert path.stat().st_size > 0, f"image file is empty: {p}"
        assert path.read_bytes() == _JPEG_MAGIC, f"file content mismatch: {p}"


def test_image_file_paths_written_png(tmp_path):
    """PNG images get a .png extension."""
    msg = _make_image_msg(image_bytes=_PNG_MAGIC, mime="image/png", msg_id="img-png-01")

    with patch.object(crc, "IMAGE_TEMP_DIR", tmp_path):
        paths = crc._image_file_paths_for_msg(msg)

    assert paths, "expected at least one file path"
    assert all(p.endswith(".png") for p in paths), (
        f"expected .png extension for PNG mime; got: {paths}"
    )


def test_image_file_paths_empty_when_no_image(tmp_path):
    """_image_file_paths_for_msg returns [] when msg has no image_b64."""
    msg = {"role": "user", "content": "no image", "ts": 5.0}
    with patch.object(crc, "IMAGE_TEMP_DIR", tmp_path):
        paths = crc._image_file_paths_for_msg(msg)
    assert paths == []


# ---------------------------------------------------------------------------
# Test 3: CLI template injects image paths into argv
# ---------------------------------------------------------------------------

def test_cli_template_includes_image_paths():
    """_render_cli_template injects {image_path} / {image_paths} into argv.

    When AGENT_CLI_CMD contains the tokens, the rendered command must
    include the actual image file paths at the token positions.
    """
    fake_cmd = "claude --print {message} --session {session_id} --image {image_path}"
    image_paths = ["/tmp/feedling_chat_images/test-img-01_0.jpg"]

    with patch.object(crc, "AGENT_CLI_CMD", fake_cmd):
        argv = crc._render_cli_template(
            message="describe this image",
            sid="sid-001",
            image_paths=image_paths,
        )

    # argv must be a list
    assert isinstance(argv, list) and argv, "rendered CLI must be a non-empty list"
    joined = " ".join(argv)
    assert image_paths[0] in joined, (
        f"image path not found in rendered CLI argv: {joined}"
    )


def test_cli_template_includes_all_image_paths():
    """_render_cli_template expands {image_paths} (plural) to all paths."""
    fake_cmd = "myagent --files {image_paths} -- {message}"
    image_paths = [
        "/tmp/feedling_chat_images/img_0.jpg",
        "/tmp/feedling_chat_images/img_1.jpg",
    ]

    with patch.object(crc, "AGENT_CLI_CMD", fake_cmd):
        argv = crc._render_cli_template(
            message="two images",
            sid="",
            image_paths=image_paths,
        )

    joined = " ".join(argv)
    for p in image_paths:
        assert p in joined, f"path {p!r} missing from argv: {joined}"


def test_cli_template_empty_image_path_when_no_images():
    """When no images are provided, {image_path} expands to empty string."""
    fake_cmd = "agent {message} --img {image_path}"

    with patch.object(crc, "AGENT_CLI_CMD", fake_cmd):
        argv = crc._render_cli_template(
            message="hello",
            sid="",
            image_paths=[],
        )

    # The --img arg will be present but its value will be empty string.
    # This ensures no crash and no residual token literal.
    joined = " ".join(argv)
    assert "__FEEDLING_IMAGE_PATH__" not in joined
    assert "{image_path}" not in joined


# ---------------------------------------------------------------------------
# Test 4: process_messages routes image turn to call_agent with payloads
# ---------------------------------------------------------------------------

def test_process_messages_image_turn_calls_agent_with_payloads(tmp_path):
    """_process_messages passes image_payloads + image_paths to call_agent
    when content_type == 'image'.
    """
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    msg = _make_image_msg(ts=9000.0, image_bytes=_JPEG_MAGIC, msg_id="img-proc-01")
    captured = {}

    def fake_call(message, images=None, image_paths=None, trace_id=None, **kwargs):
        captured["message"] = message
        captured["images"] = images
        captured["image_paths"] = image_paths
        return {"messages": ["I see the image."]}

    with patch.object(crc, "IMAGE_TEMP_DIR", tmp_path), \
         patch.object(crc, "call_agent", side_effect=fake_call), \
         patch.object(crc, "post_reply", return_value={"id": "reply-img-01"}):
        result_ts = crc._process_messages([msg])

    assert result_ts == pytest.approx(9000.0)
    assert captured.get("images"), "call_agent must receive non-empty images list"
    assert captured.get("image_paths"), "call_agent must receive non-empty image_paths list"
    # Verify the image payload has the expected fields
    payload = captured["images"][0]
    assert "mime_type" in payload
    assert "data" in payload
    assert "data_url" in payload


def test_process_messages_image_turn_preserves_caption(tmp_path):
    """带文字说明的图片 turn：content（caption）不被 IMAGE_PLACEHOLDER 覆盖，
    agent 收到用户的真实问题而非占位符（Codex P2 修复）。"""
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    msg = _make_image_msg(ts=9100.0, image_bytes=_JPEG_MAGIC, msg_id="img-cap-01")
    msg["content"] = "what is wrong here?"  # enclave history 解出的 caption
    captured = {}

    def fake_call(message, images=None, image_paths=None, trace_id=None, **kwargs):
        captured["message"] = message
        return {"messages": ["I see the image."]}

    with patch.object(crc, "IMAGE_TEMP_DIR", tmp_path), \
         patch.object(crc, "call_agent", side_effect=fake_call), \
         patch.object(crc, "post_reply", return_value={"id": "reply-cap-01"}):
        crc._process_messages([msg])

    assert "what is wrong here?" in captured.get("message", ""), (
        f"caption 应随图片传给 agent，实际 message={captured.get('message')!r}"
    )
    assert captured["message"] != crc.IMAGE_PLACEHOLDER, "content 不应被占位符整体覆盖"


def test_process_messages_image_turn_no_caption_uses_placeholder(tmp_path):
    """无文字的图片 turn 仍回退到 IMAGE_PLACEHOLDER（向后兼容）。"""
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    msg = _make_image_msg(ts=9200.0, image_bytes=_JPEG_MAGIC, msg_id="img-nocap-01")
    msg["content"] = ""  # 没有 caption
    captured = {}

    def fake_call(message, images=None, image_paths=None, trace_id=None, **kwargs):
        captured["message"] = message
        return {"messages": ["I see the image."]}

    with patch.object(crc, "IMAGE_TEMP_DIR", tmp_path), \
         patch.object(crc, "call_agent", side_effect=fake_call), \
         patch.object(crc, "post_reply", return_value={"id": "reply-nocap-01"}):
        crc._process_messages([msg])

    assert crc.IMAGE_PLACEHOLDER in captured.get("message", ""), (
        f"无 caption 时应含占位符，实际 {captured.get('message')!r}"
    )
