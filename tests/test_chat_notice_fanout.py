"""record_runtime_error 扇出到 user_notices（spec Phase B / B3）。

Run:  python -m pytest tests/test_chat_notice_fanout.py -q
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from conftest import seed_user  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import config_store  # noqa: E402
from notices import core as notices_core  # noqa: E402


def _uid():
    return "usr_" + uuid.uuid4().hex[:12]


def _seed_model_api(uid):
    seed_user(uid)
    store = core_store.get_store(uid)
    config_store._save_model_api_config(
        store, {"provider": "anthropic", "model": "claude-3-5-sonnet-latest"})
    config_store._ensure_model_api_runtime_profile(store)
    return store


def test_error_fans_out_to_notice():
    uid = _uid(); store = _seed_model_api(uid)
    config_store.record_runtime_error(
        store, error="403 预扣费额度失败", error_class="quota_insufficient")
    rows = {r["dedupe_key"]: r for r in db.log_read_all(uid, notices_core.NOTICES_STREAM)}
    assert "chat:quota_insufficient" in rows
    n = rows["chat:quota_insufficient"]
    assert n["source"] == "chat" and n["resolved"] is False
    assert n["detail"] == "403 预扣费额度失败"


def test_clear_resolves_chat_notices():
    uid = _uid(); store = _seed_model_api(uid)
    config_store.record_runtime_error(
        store, error="403 预扣费额度失败", error_class="quota_insufficient")
    config_store.record_runtime_error(store, error="", error_class="")   # 清空
    rows = db.log_read_all(uid, notices_core.NOTICES_STREAM)
    assert all(r["resolved"] for r in rows if r["dedupe_key"].startswith("chat:"))


def test_unknown_error_class_falls_back_to_unknown_dedupe_key():
    """error_class 空字符串时 dedupe_key 不能是 'chat:'（会和 resolve 前缀撞、
    也不利于去重），要落到 'chat:unknown'。"""
    uid = _uid(); store = _seed_model_api(uid)
    config_store.record_runtime_error(store, error="mystery failure", error_class="")
    rows = {r["dedupe_key"]: r for r in db.log_read_all(uid, notices_core.NOTICES_STREAM)}
    assert "chat:unknown" in rows
    assert rows["chat:unknown"]["blame"] == "system"


def test_repeated_same_error_increments_occurrences_not_duplicates():
    uid = _uid(); store = _seed_model_api(uid)
    config_store.record_runtime_error(
        store, error="429 too many requests", error_class="rate_limited")
    config_store.record_runtime_error(
        store, error="429 too many requests", error_class="rate_limited")
    rows = [r for r in db.log_read_all(uid, notices_core.NOTICES_STREAM)
            if r["dedupe_key"] == "chat:rate_limited"]
    assert len(rows) == 1
    assert rows[0]["occurrences"] == 2
