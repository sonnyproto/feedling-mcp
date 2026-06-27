"""Unit tests for the onboarding-archive R2 layer.

Fake S3 client stands in for boto3 (no real network). Mirrors
tests/test_object_storage.py's _FakeS3 but adds upload_fileobj.
"""

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import object_storage  # noqa: E402
from onboarding_archive import storage  # noqa: E402


class _FakeS3:
    def __init__(self):
        self.store: dict[tuple, bytes] = {}

    def upload_fileobj(self, Fileobj, Bucket, Key, ExtraArgs=None):
        self.store[(Bucket, Key)] = Fileobj.read()

    def list_objects_v2(self, Bucket, Prefix, **kw):
        keys = [k for (b, k) in self.store if b == Bucket and k.startswith(Prefix)]
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def delete_objects(self, Bucket, Delete, **kw):
        for o in Delete["Objects"]:
            self.store.pop((Bucket, o["Key"]), None)
        return {}


@pytest.fixture
def fake(monkeypatch):
    client = _FakeS3()
    monkeypatch.setenv("R2_ENDPOINT", "https://acct.r2.cloudflarestorage.com")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "sk")
    monkeypatch.setenv("R2_USER_LOGS_BUCKET", "io-user-logs")
    monkeypatch.setattr(object_storage, "_client", lambda: client)
    return client


def test_enabled_requires_creds_and_bucket(monkeypatch):
    for k in ("R2_ENDPOINT", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
              "R2_SECRET_ACCESS_KEY", "R2_USER_LOGS_BUCKET"):
        monkeypatch.delenv(k, raising=False)
    assert storage.enabled() is False
    monkeypatch.setenv("R2_ENDPOINT", "https://x")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "sk")
    assert storage.enabled() is False  # bucket still missing
    monkeypatch.setenv("R2_USER_LOGS_BUCKET", "io-user-logs")
    assert storage.enabled() is True


def test_archive_key_format():
    assert storage.archive_key("u1", "abc", "chat.json") == "onboarding/u1/abc/chat.json"


def test_put_archive_streams_bytes(fake):
    key = storage.put_archive("u1", "a1", "chat.json", io.BytesIO(b"hello"), "application/json")
    assert key == "onboarding/u1/a1/chat.json"
    assert fake.store[("io-user-logs", "onboarding/u1/a1/chat.json")] == b"hello"


def test_delete_user_archives_removes_only_that_prefix(fake):
    storage.put_archive("u1", "a1", "f.json", io.BytesIO(b"a"), "application/json")
    storage.put_archive("u1", "a2", "g.json", io.BytesIO(b"b"), "application/json")
    storage.put_archive("u2", "a1", "h.json", io.BytesIO(b"c"), "application/json")
    storage.delete_user_archives("u1")
    assert ("io-user-logs", "onboarding/u1/a1/f.json") not in fake.store
    assert ("io-user-logs", "onboarding/u1/a2/g.json") not in fake.store
    assert ("io-user-logs", "onboarding/u2/a1/h.json") in fake.store


def test_delete_user_archives_propagates_errors(fake):
    """R2 list 失败时应抛出，不再吞异常（P1 修复）。"""
    def _boom(**kw):
        raise RuntimeError("list failed")
    fake.list_objects_v2 = _boom
    with pytest.raises(RuntimeError):
        storage.delete_user_archives("u1")


def test_delete_user_archives_raises_on_partial_delete_errors(fake):
    storage.put_archive("u1", "a1", "f.json", io.BytesIO(b"a"), "application/json")
    fake.delete_objects = lambda **kw: {"Errors": [{"Key": "onboarding/u1/a1/f.json",
                                                    "Code": "AccessDenied"}]}
    with pytest.raises(RuntimeError):
        storage.delete_user_archives("u1")
