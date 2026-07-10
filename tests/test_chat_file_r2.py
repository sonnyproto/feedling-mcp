"""db.py chat file-body R2 offload.

A content_type="file" body_ct is offloaded to R2; the row keeps a body_key
pointer and chat_load reconstitutes body_ct transparently. Text/image messages
stay inline, and an unconfigured/failed R2 falls back to inline. A fake S3 client
stands in for boto3 (mirrors test_frame_r2.py). Legacy (R2-off) behaviour and the
non-file paths are unchanged.
"""

import base64
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
import object_storage  # noqa: E402

from conftest import seed_user  # noqa: E402
from test_frame_r2 import _FakeS3  # noqa: E402  reuse the fake S3


_BUCKET = "io-chat-files"


def _enable_r2(monkeypatch, client):
    monkeypatch.setenv("R2_ENDPOINT", "https://acct.r2.cloudflarestorage.com")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "sk")
    monkeypatch.setenv("R2_CHAT_FILES_BUCKET", _BUCKET)
    monkeypatch.setattr(object_storage, "_client", lambda: client)


def _uid() -> str:
    return f"u_{uuid.uuid4().hex[:10]}"


def _file_doc(uid: str, mid: str, body: bytes = b"PK\x03\x04docx-bytes") -> dict:
    return {
        "id": mid, "role": "user", "ts": 1.0, "source": "model_api",
        "content_type": "file", "file_name": "报告.docx", "file_mime": "application/octet-stream",
        "body_ct": base64.b64encode(body).decode(),
        "nonce": base64.b64encode(b"123456789012").decode(),
        "K_user": base64.b64encode(b"user-key").decode(),
        "K_enclave": base64.b64encode(b"enc-key").decode(),
        "visibility": "shared", "owner_user_id": uid,
    }


def _raw_doc(uid: str, mid: str) -> dict | None:
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT doc FROM chat_messages WHERE user_id=%s AND msg_id=%s", (uid, mid)
        ).fetchone()
    return row[0] if row else None


def test_file_body_offloaded_to_r2_and_reconstituted(backend_env, monkeypatch):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    body = b"PK\x03\x04the-real-file-bytes"
    db.chat_append(uid, mid, 1.0, _file_doc(uid, mid, body), 100)

    # Stored row is the slim pointer shape: body_key present, body_ct gone.
    raw = _raw_doc(uid, mid)
    assert raw["body_key"] == f"chatfiles/{uid}/{mid}"
    assert "body_ct" not in raw
    # The ciphertext lives in R2 under that key.
    assert client.store[(_BUCKET, f"chatfiles/{uid}/{mid}")] == body
    # chat_load reconstitutes a normal inline file message.
    loaded = {m["id"]: m for m in db.chat_load(uid)}
    assert base64.b64decode(loaded[mid]["body_ct"]) == body
    assert "body_key" not in loaded[mid]


def test_text_message_stays_inline(backend_env, monkeypatch):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    doc = _file_doc(uid, mid)
    doc["content_type"] = "text"
    db.chat_append(uid, mid, 1.0, doc, 100)
    raw = _raw_doc(uid, mid)
    assert "body_ct" in raw and "body_key" not in raw          # inline
    assert not client.store                                    # nothing uploaded


def test_upload_failure_falls_back_to_inline(backend_env, monkeypatch):
    client = _FakeS3(fail_put=True)
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    db.chat_append(uid, mid, 1.0, _file_doc(uid, mid), 100)
    raw = _raw_doc(uid, mid)
    assert "body_ct" in raw and "body_key" not in raw          # stayed inline, readable
    assert not client.store


def test_disabled_r2_stays_inline(backend_env, monkeypatch):
    # No R2 env → chat_files_enabled() False → exact legacy behaviour.
    monkeypatch.delenv("R2_CHAT_FILES_BUCKET", raising=False)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    db.chat_append(uid, mid, 1.0, _file_doc(uid, mid), 100)
    raw = _raw_doc(uid, mid)
    assert "body_ct" in raw and "body_key" not in raw


def test_chat_clear_purges_r2(backend_env, monkeypatch):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    db.chat_append(uid, mid, 1.0, _file_doc(uid, mid), 100)
    assert client.store
    db.chat_clear(uid)
    assert not client.store                                    # R2 objects purged


def test_chat_delete_purges_r2(backend_env, monkeypatch):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    db.chat_append(uid, mid, 1.0, _file_doc(uid, mid), 100)
    assert client.store
    assert db.chat_delete(uid, mid) is True
    assert not client.store
