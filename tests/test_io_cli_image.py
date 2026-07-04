"""io_cli `--include-image` must hand a CLI vision agent a FILE PATH, not base64.

Emitting the decrypted `image_b64` inline on stdout is useless to a vision model
(it cannot decode a base64 string in its head) and bloats the tool output. The
consumer already writes decrypted chat/screen images to IMAGE_TEMP_DIR and lets
claude Read them; `screen-read`/`photo-read --include-image` must do the same so
the agent can actually SEE the screen frame / photo it fetched.
"""
import base64
import json
import os
import sys
from pathlib import Path

TOOLS = Path(__file__).parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

import io_cli  # noqa: E402

_PIXELS = b"\xff\xd8\xff\xe0FAKEJPEGBYTES\xff\xd9"


def test_materialize_writes_file_and_drops_base64(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_TEMP_DIR", str(tmp_path))
    body = {
        "ocr_text": "hello",
        "image_b64": base64.b64encode(_PIXELS).decode(),
        "image_mime": "image/jpeg",
    }
    out = io_cli._materialize_decrypted_image("screen_frame42", body)

    # base64 is gone from the emitted dict (no giant useless text blob)
    assert "image_b64" not in out
    # a file path is handed back instead
    path = out["image_file"]
    assert path.endswith(".jpg")
    # the file exists inside IMAGE_TEMP_DIR and holds the real decoded bytes
    assert Path(path).parent == tmp_path
    assert Path(path).read_bytes() == _PIXELS
    # non-image metadata is preserved
    assert out["ocr_text"] == "hello"


def test_materialize_png_extension(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_TEMP_DIR", str(tmp_path))
    body = {"image_b64": base64.b64encode(_PIXELS).decode(), "image_mime": "image/png"}
    out = io_cli._materialize_decrypted_image("photo_1", body)
    assert out["image_file"].endswith(".png")


def test_materialize_noop_without_image(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_TEMP_DIR", str(tmp_path))
    body = {"ocr_text": "only text, pixels gated off"}
    out = io_cli._materialize_decrypted_image("screen_x", body)
    assert out == body  # unchanged; no image_file invented


def test_materialize_handles_data_url_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_TEMP_DIR", str(tmp_path))
    b64 = base64.b64encode(_PIXELS).decode()
    body = {"image_b64": f"data:image/jpeg;base64,{b64}", "image_mime": "image/jpeg"}
    out = io_cli._materialize_decrypted_image("screen_y", body)
    assert Path(out["image_file"]).read_bytes() == _PIXELS
