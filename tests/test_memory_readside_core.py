from __future__ import annotations

import sys
import types
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import memory_readside_core as readside_core  # noqa: E402


def _moment(
    mid: str,
    *,
    owner: str = "usr_core",
    visibility: str = "shared",
    k_enclave: bool = True,
    status: str | None = "active",
    salience: str | None = "medium",
    importance: float | None = 0.5,
    occurred_at: str = "2026-06-20T10:00:00",
    is_open_thread: bool = False,
    archived: bool = False,
) -> dict:
    moment = {
        "v": 1,
        "id": mid,
        "owner_user_id": owner,
        "visibility": visibility,
        "body_ct": f"ct_{mid}",
        "nonce": f"nonce_{mid}",
        "K_user": f"ku_{mid}",
        "occurred_at": occurred_at,
        "created_at": occurred_at,
        "updated_at": occurred_at,
        "type": "fact",
        "source": "test",
    }
    if k_enclave:
        moment["K_enclave"] = f"ke_{mid}"
    if status is not None:
        moment["status"] = status
    if salience is not None:
        moment["salience"] = salience
    if importance is not None:
        moment["importance"] = importance
    if is_open_thread:
        moment["is_open_thread"] = True
    if archived:
        moment["archived_at"] = "2026-06-20T11:00:00"
    return moment


def test_index_core_prefilters_sorts_caps_and_reports_card_count(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_core")
    moments = [
        _moment("open_old", salience="medium", importance=0.1, occurred_at="2026-06-19T10:00:00", is_open_thread=True),
        _moment("high_new", salience="high", importance=0.9, occurred_at="2026-06-20T10:00:00"),
        _moment("local", visibility="local_only"),
        _moment("no_enclave", k_enclave=False),
        _moment("archived", archived=True),
        _moment("deleted", status="deleted"),
        _moment("superseded", status="superseded"),
        _moment("other_user", owner="usr_other"),
    ]
    moments.extend(
        _moment(f"bulk_{idx:02d}", salience="low", importance=0.1, occurred_at=f"2026-06-18T{idx:02d}:00:00")
        for idx in range(60)
    )
    monkeypatch.setattr(readside_core.memory_service, "_load_moments", lambda _store: moments)
    captured = {}

    def fake_enclave(api_key, candidates, *, operation, payload=None):
        captured["api_key"] = api_key
        captured["operation"] = operation
        captured["ids"] = [m["id"] for m in candidates]
        captured["payload"] = dict(payload or {})
        return {"items": [{"id": m["id"], "summary": m["id"]} for m in candidates]}

    monkeypatch.setattr(readside_core, "post_enclave_readside", fake_enclave)

    body = readside_core.memory_index_core(store, "key_core", {"limit": 50})

    assert captured["api_key"] == "key_core"
    assert captured["operation"] == "index"
    assert captured["payload"]["include_sensitive"] is False
    assert len(captured["ids"]) == 50
    assert captured["ids"][:2] == ["open_old", "high_new"]
    assert body["user_card_count"] == 62
    assert body["limit"] == 50
    assert body["items"][0]["id"] == "open_old"
    assert "local" not in captured["ids"]
    assert "no_enclave" not in captured["ids"]
    assert "archived" not in captured["ids"]
    assert "deleted" not in captured["ids"]
    assert "superseded" not in captured["ids"]
    assert "other_user" not in captured["ids"]


def test_fetch_core_splits_missing_unavailable_and_preserves_order(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_core")
    moments = [
        _moment("ok_b"),
        _moment("local", visibility="local_only"),
        _moment("ok_a"),
        _moment("archived", archived=True),
        _moment("superseded", status="superseded"),
        _moment("other_user", owner="usr_other"),
    ]
    monkeypatch.setattr(readside_core.memory_service, "_load_moments", lambda _store: moments)
    captured = {}

    def fake_enclave(api_key, candidates, *, operation, payload=None):
        captured["ids"] = [m["id"] for m in candidates]
        return {
            "items": [{"id": m["id"], "summary": f"summary {m['id']}"} for m in candidates],
            "unavailable_ids": [],
        }

    monkeypatch.setattr(readside_core, "post_enclave_readside", fake_enclave)

    body = readside_core.memory_fetch_core(
        store,
        "key_core",
        {"ids": ["ok_a", "missing", "local", "ok_b", "archived", "superseded", "other_user"]},
    )

    assert captured["ids"] == ["ok_a", "ok_b"]
    assert [item["id"] for item in body["items"]] == ["ok_a", "ok_b"]
    assert body["missing_ids"] == ["missing", "other_user"]
    assert body["unavailable_ids"] == ["local", "archived", "superseded"]
