"""GET /v1/notices：鉴权 + 快照过滤 + include_resolved + 排序（spec Phase B / B2）。

Run:  python -m pytest tests/test_notices_route.py -q
"""
from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from asgi_test_client import make_client  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _register():
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"})
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _doc(key, ts, *, resolved, resolved_ts=None):
    return {
        "notice_id": "ntc_" + key.replace(":", "_"), "source": "chat",
        "error_class": "quota_insufficient", "blame": "user_provider",
        "severity": "error", "user_text": "x", "detail": "", "dedupe_key": key,
        "occurrences": 1, "first_ts": ts, "last_ts": ts,
        "resolved": resolved, "resolved_ts": resolved_ts}


def _seed(uid):
    now = time.time()
    db.log_append(uid, "user_notices", _doc("chat:active", now, resolved=False),
                  ts=now, item_key="chat:active")
    db.log_append(uid, "user_notices", _doc("chat:newer", now + 5, resolved=False),
                  ts=now + 5, item_key="chat:newer")
    db.log_append(uid, "user_notices",
                  _doc("chat:recent", now - 10, resolved=True, resolved_ts=now - 10),
                  ts=now - 10, item_key="chat:recent")
    db.log_append(uid, "user_notices",
                  _doc("chat:old", now - 30 * 86400, resolved=True,
                       resolved_ts=now - 30 * 86400),
                  ts=now - 30 * 86400, item_key="chat:old")


def test_requires_auth(backend_env):
    res = make_client().get("/v1/notices")
    assert res.status_code == 401


def test_snapshot_filter_and_sort(backend_env):
    uid, key = _register()
    _seed(uid)
    res = make_client().get("/v1/notices", headers={"X-API-Key": key})
    assert res.status_code == 200
    keys = [n["dedupe_key"] for n in res.get_json()["notices"]]
    # 活跃全给 + recent resolved 在 7d 内给 + old 超窗被滤；按 last_ts 倒序
    assert keys == ["chat:newer", "chat:active", "chat:recent"]


def test_include_resolved_false_hides_resolved(backend_env):
    uid, key = _register()
    _seed(uid)
    res = make_client().get("/v1/notices?include_resolved=false",
                            headers={"X-API-Key": key})
    assert res.status_code == 200
    keys = [n["dedupe_key"] for n in res.get_json()["notices"]]
    assert keys == ["chat:newer", "chat:active"]


def test_notice_doc_shape(backend_env):
    uid, key = _register()
    _seed(uid)
    res = make_client().get("/v1/notices", headers={"X-API-Key": key})
    n = res.get_json()["notices"][0]
    assert set(n.keys()) == {
        "notice_id", "source", "error_class", "blame", "severity", "user_text",
        "detail", "dedupe_key", "occurrences", "first_ts", "last_ts",
        "resolved", "resolved_ts"}
