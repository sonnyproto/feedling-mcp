"""db.py chat file-body R2 offload (lazy hydration at delivery exits).

A content_type="file" body_ct is offloaded to R2; the chat_messages row keeps a
slim pointer (body_key + body_ct_len). chat_load returns POINTERS — the heavy
ciphertext is fetched lazily only at read exits that deliver a body (poll claim,
a history page that includes the body, single message_body). Text/image messages
and, when R2 is unconfigured, everything stay inline. A fake S3 stands in for
boto3 (reuses test_frame_r2). Mirrors how large image bodies are omitted and
lazily re-fetched.
"""

import base64
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
import object_storage  # noqa: E402
from chat import service as chat_service  # noqa: E402

from conftest import seed_user  # noqa: E402
from test_frame_r2 import _FakeS3  # noqa: E402  reuse the fake S3


_BUCKET = "io-user-attachments"


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


def test_offload_stores_pointer_and_chat_load_is_lazy(backend_env, monkeypatch):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    body = b"PK\x03\x04the-real-file-bytes"
    db.chat_append(uid, mid, 1.0, _file_doc(uid, mid, body), 100)

    # Stored row = slim pointer: body_key + body_ct_len, body_ct gone.
    raw = _raw_doc(uid, mid)
    assert raw["body_key"] == f"chatfiles/{uid}/{mid}"
    assert raw["body_ct_len"] == len(base64.b64encode(body).decode())
    assert "body_ct" not in raw
    # Ciphertext lives in R2.
    assert client.store[(_BUCKET, f"chatfiles/{uid}/{mid}")] == body
    # chat_load is LAZY: returns the pointer, does NOT reconstitute.
    loaded = {m["id"]: m for m in db.chat_load(uid)}
    assert loaded[mid].get("body_key") and loaded[mid].get("body_ct") is None


def test_hydrate_helper_reconstitutes(backend_env, monkeypatch):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    body = b"hydrate-me"
    db.chat_append(uid, mid, 1.0, _file_doc(uid, mid, body), 100)
    pointer = _raw_doc(uid, mid)
    full = db.hydrate_chat_file_body(uid, pointer)
    assert base64.b64decode(full["body_ct"]) == body
    assert "body_key" not in full


def test_history_item_hydrates_when_body_included(backend_env, monkeypatch):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    body = b"included-body"
    db.chat_append(uid, mid, 1.0, _file_doc(uid, mid, body), 100)
    item = chat_service._chat_history_item(_raw_doc(uid, mid), include_image_body=True)
    assert base64.b64decode(item["body_ct"]) == body
    assert "body_key" not in item
    assert not item.get("body_omitted")


def test_history_item_omits_large_file_without_fetch(backend_env, monkeypatch):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    big = b"x" * 300_000  # > CHAT_HISTORY_INLINE_BODY_CT_MAX (262144)
    db.chat_append(uid, mid, 1.0, _file_doc(uid, mid, big), 100)
    pointer = _raw_doc(uid, mid)
    item = chat_service._chat_history_item(pointer, include_image_body=False)
    # Omitted → reported from the pointer's stored length, no R2 fetch, no body.
    assert item["body_omitted"] is True
    assert item.get("body_ct") is None and "body_key" not in item
    assert item["body_ct_len"] == pointer["body_ct_len"]


def test_phase3_preserves_concurrent_metadata(backend_env, monkeypatch):
    # Simulate another worker writing reply metadata DURING the R2 upload; the
    # atomic pointer flip must not clobber it (P1b regression).
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex

    class _RacingS3(_FakeS3):
        def put_object(self, Bucket, Key, Body, **kw):
            db.chat_update_metadata(uid, mid, {"reply_status": "replied", "reply_message_id": "r1"})
            return super().put_object(Bucket, Key, Body, **kw)

    _enable_r2(monkeypatch, _RacingS3())
    db.chat_append(uid, mid, 1.0, _file_doc(uid, mid), 100)
    raw = _raw_doc(uid, mid)
    assert raw["reply_status"] == "replied"        # concurrent write survived
    assert raw["reply_message_id"] == "r1"
    assert raw.get("body_key") and "body_ct" not in raw  # and the flip still happened


def test_text_message_stays_inline(backend_env, monkeypatch):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    doc = _file_doc(uid, mid); doc["content_type"] = "text"
    db.chat_append(uid, mid, 1.0, doc, 100)
    raw = _raw_doc(uid, mid)
    assert "body_ct" in raw and "body_key" not in raw
    assert not client.store


def test_upload_failure_falls_back_to_inline(backend_env, monkeypatch):
    _enable_r2(monkeypatch, _FakeS3(fail_put=True))
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    db.chat_append(uid, mid, 1.0, _file_doc(uid, mid), 100)
    raw = _raw_doc(uid, mid)
    assert "body_ct" in raw and "body_key" not in raw   # stayed inline, readable


def test_disabled_r2_stays_inline(backend_env, monkeypatch):
    monkeypatch.delenv("R2_CHAT_FILES_BUCKET", raising=False)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    db.chat_append(uid, mid, 1.0, _file_doc(uid, mid), 100)
    raw = _raw_doc(uid, mid)
    assert "body_ct" in raw and "body_key" not in raw


def test_chat_clear_and_delete_purge_r2(backend_env, monkeypatch):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid)
    m1, m2 = uuid.uuid4().hex, uuid.uuid4().hex
    db.chat_append(uid, m1, 1.0, _file_doc(uid, m1), 100)
    db.chat_append(uid, m2, 2.0, _file_doc(uid, m2), 100)
    assert len(client.store) == 2
    assert db.chat_delete(uid, m1) is True
    assert (_BUCKET, f"chatfiles/{uid}/{m1}") not in client.store
    db.chat_clear(uid)
    assert not client.store
