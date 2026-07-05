"""Pure-function visual/frame plaintext parsing (no Flask/FastAPI/httpx).

Moved verbatim from enclave_app.py (old L1802-1849), dropping the leading
underscore from the names that are now this module's public surface.
"""

from __future__ import annotations

import base64
import json
from typing import Any


def raw_image_mime(data: bytes) -> str | None:
    """Return the MIME type when plaintext is a recognized raw image."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return "image/heic"
        if brand in {b"avif", b"avis"}:
            return "image/avif"
    return None


IMAGE_EXTENSION_BY_MIME = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/avif": "avif",
}


def parse_visual_plaintext(plaintext: bytes) -> dict[str, Any]:
    """Decode a screen-frame JSON wrapper or a raw encrypted photo.

    Screen capture envelopes contain a UTF-8 JSON object whose ``image`` field
    is base64 JPEG. Perception photo envelopes reuse the same ciphertext
    channel but encrypt the image bytes directly. Only recognized image file
    signatures take the raw-photo fallback so malformed frame JSON still fails
    closed instead of being forwarded to a vision provider as arbitrary bytes.
    """
    try:
        inner = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        image_mime = raw_image_mime(plaintext)
        if image_mime is None:
            raise
        return {
            "image": base64.b64encode(plaintext).decode("ascii"),
            "image_mime": image_mime,
        }
    if not isinstance(inner, dict):
        raise ValueError("visual plaintext is not an object")
    return inner
