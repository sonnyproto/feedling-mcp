from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from flask import Flask


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import enclave_app  # noqa: E402
from memory import routes as memory_routes  # noqa: E402


def _moment(
    mid: str,
    *,
    owner: str = "usr_readside",
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


@pytest.fixture()
def client(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_readside")
    monkeypatch.setattr(memory_routes.auth, "require_user", lambda: store)
    monkeypatch.setattr(memory_routes.auth, "_extract_api_key", lambda: "key_readside")
    app = Flask(__name__)
    app.register_blueprint(memory_routes.bp)
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c, store


def test_memory_index_prefilters_top50_and_calls_enclave(client, monkeypatch):
    c, store = client
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
    monkeypatch.setattr(memory_routes.memory_service, "_load_moments", lambda _store: moments)
    captured = {}

    def fake_enclave(api_key, candidates, *, operation, payload=None):
        captured["api_key"] = api_key
        captured["operation"] = operation
        captured["ids"] = [m["id"] for m in candidates]
        captured["payload"] = dict(payload or {})
        return {"items": [{"id": m["id"], "summary": m["id"]} for m in candidates]}

    monkeypatch.setattr(memory_routes, "_memory_readside_post_enclave", fake_enclave)

    res = c.post("/v1/memory/index", json={}, headers={"X-API-Key": "key_readside"})

    assert res.status_code == 200
    assert captured["api_key"] == "key_readside"
    assert captured["operation"] == "index"
    assert captured["payload"]["include_sensitive"] is False
    assert len(captured["ids"]) == 50
    assert captured["ids"][:2] == ["open_old", "high_new"]
    assert "local" not in captured["ids"]
    assert "no_enclave" not in captured["ids"]
    assert "archived" not in captured["ids"]
    assert "deleted" not in captured["ids"]
    assert "superseded" not in captured["ids"]
    assert "other_user" not in captured["ids"]


def test_memory_fetch_splits_missing_unavailable_and_preserves_order(client, monkeypatch):
    c, _store = client
    moments = [
        _moment("ok_b"),
        _moment("local", visibility="local_only"),
        _moment("ok_a"),
        _moment("archived", archived=True),
        _moment("superseded", status="superseded"),
        _moment("other_user", owner="usr_other"),
    ]
    monkeypatch.setattr(memory_routes.memory_service, "_load_moments", lambda _store: moments)
    captured = {}

    def fake_enclave(api_key, candidates, *, operation, payload=None):
        captured["ids"] = [m["id"] for m in candidates]
        return {
            "items": [{"id": m["id"], "summary": f"summary {m['id']}"} for m in candidates],
            "unavailable_ids": [],
        }

    monkeypatch.setattr(memory_routes, "_memory_readside_post_enclave", fake_enclave)

    res = c.post(
        "/v1/memory/fetch",
        json={"ids": ["ok_a", "missing", "local", "ok_b", "archived", "superseded", "other_user"]},
        headers={"X-API-Key": "key_readside"},
    )

    assert res.status_code == 200
    body = res.get_json()
    assert captured["ids"] == ["ok_a", "ok_b"]
    assert [item["id"] for item in body["items"]] == ["ok_a", "ok_b"]
    assert body["missing_ids"] == ["missing", "other_user"]
    assert body["unavailable_ids"] == ["local", "archived", "superseded"]


def test_enclave_index_item_hides_body_only_fields():
    item = enclave_app._build_memory_index_item(
        {
            "id": "mem_1",
            "status": "active",
            "salience": "high",
            "is_open_thread": True,
            "score": 0.91,
        },
        {
            "summary": "She needs presence first.",
            "bucket_refs": ["comfort"],
            "verbatim": "Do not expose this in index.",
            "her_quote": "Do not expose this either.",
            "follow_up": "Only fetch should see this.",
            "sensitive_scope": "xp_private_detail",
        },
    )

    assert item == {
        "id": "mem_1",
        "summary": "She needs presence first.",
        "bucket_refs": ["comfort"],
        "status": "active",
        "salience": "high",
        "is_open_thread": True,
        "is_sensitive": True,
        "score": 0.91,
    }


def test_enclave_index_filters_sensitive_items_by_default(monkeypatch):
    monkeypatch.setattr(
        enclave_app,
        "_memory_readside_auth_context",
        lambda: ("key_readside", "usr_readside", object(), None),
    )
    monkeypatch.setattr(
        enclave_app,
        "_memory_readside_decrypt_items",
        lambda moments, authorized_user_id, content_sk, *, item_builder: (
            [
                {"id": "plain", "summary": "Plain memory.", "is_sensitive": False},
                {"id": "sensitive", "summary": "Private memory.", "is_sensitive": True},
            ],
            [],
        ),
    )

    with enclave_app.app.test_client() as c:
        default_res = c.post("/v1/memory/index", json={"moments": [{"id": "plain"}, {"id": "sensitive"}]})
        sensitive_res = c.post(
            "/v1/memory/index",
            json={"moments": [{"id": "plain"}, {"id": "sensitive"}], "include_sensitive": True},
        )

    assert default_res.status_code == 200
    assert [item["id"] for item in default_res.get_json()["items"]] == ["plain"]
    assert sensitive_res.status_code == 200
    assert [item["id"] for item in sensitive_res.get_json()["items"]] == ["plain", "sensitive"]


def test_enclave_index_and_fetch_honor_payload_limit_above_50(monkeypatch):
    monkeypatch.delenv("FEEDLING_MEMORY_READSIDE_LIMIT", raising=False)
    monkeypatch.delenv("FEEDLING_MEMORY_READSIDE_HARD_MAX", raising=False)
    monkeypatch.setattr(
        enclave_app,
        "_memory_readside_auth_context",
        lambda: ("key_readside", "usr_readside", object(), None),
    )
    captured_lengths = []

    def fake_decrypt(moments, authorized_user_id, content_sk, *, item_builder):
        captured_lengths.append(len(moments))
        return ([{"id": str(moment.get("id")), "summary": str(moment.get("id"))} for moment in moments], [])

    monkeypatch.setattr(enclave_app, "_memory_readside_decrypt_items", fake_decrypt)
    payload = {"limit": 120, "moments": [{"id": f"mem_{idx:03d}"} for idx in range(130)]}

    with enclave_app.app.test_client() as c:
        index_res = c.post("/v1/memory/index", json=payload)
        fetch_res = c.post("/v1/memory/fetch", json=payload)

    assert index_res.status_code == 200
    assert fetch_res.status_code == 200
    assert captured_lengths == [120, 120]


def test_enclave_limit_zero_uses_hard_max_instead_of_unbounded(monkeypatch):
    monkeypatch.setenv("FEEDLING_MEMORY_READSIDE_HARD_MAX", "7")
    monkeypatch.setattr(
        enclave_app,
        "_memory_readside_auth_context",
        lambda: ("key_readside", "usr_readside", object(), None),
    )
    captured_lengths = []

    def fake_decrypt(moments, authorized_user_id, content_sk, *, item_builder):
        captured_lengths.append(len(moments))
        return ([{"id": str(moment.get("id")), "summary": str(moment.get("id"))} for moment in moments], [])

    monkeypatch.setattr(enclave_app, "_memory_readside_decrypt_items", fake_decrypt)
    payload = {"limit": 0, "moments": [{"id": f"mem_{idx:03d}"} for idx in range(20)]}

    with enclave_app.app.test_client() as c:
        res = c.post("/v1/memory/index", json=payload)

    assert res.status_code == 200
    assert captured_lengths == [7]


def test_enclave_negative_env_limit_falls_back_to_default_not_full_open(monkeypatch):
    monkeypatch.setenv("FEEDLING_MEMORY_READSIDE_LIMIT", "-1")
    monkeypatch.delenv("FEEDLING_MEMORY_READSIDE_HARD_MAX", raising=False)
    monkeypatch.setattr(
        enclave_app,
        "_memory_readside_auth_context",
        lambda: ("key_readside", "usr_readside", object(), None),
    )
    captured_lengths = []

    def fake_decrypt(moments, authorized_user_id, content_sk, *, item_builder):
        captured_lengths.append(len(moments))
        return ([{"id": str(moment.get("id")), "summary": str(moment.get("id"))} for moment in moments], [])

    monkeypatch.setattr(enclave_app, "_memory_readside_decrypt_items", fake_decrypt)

    with enclave_app.app.test_client() as c:
        res = c.post("/v1/memory/index", json={"moments": [{"id": f"mem_{idx:03d}"} for idx in range(70)]})

    assert res.status_code == 200
    assert captured_lengths == [50]


def test_enclave_fetch_item_returns_full_card_without_sensitive_scope():
    item = enclave_app._build_memory_fetch_item(
        {"id": "mem_1", "status": "active", "salience": "high", "source": "chat"},
        {
            "summary": "She needs presence first.",
            "verbatim": "I wanted someone to stay.",
            "bucket_refs": ["comfort"],
            "follow_up": "Start with comfort.",
            "context": "Low mood chat.",
            "source_type": "chat",
            "sensitive_scope": "xp_private_detail",
        },
    )

    assert item == {
        "id": "mem_1",
        "summary": "She needs presence first.",
        "verbatim": "I wanted someone to stay.",
        "bucket_refs": ["comfort"],
        "status": "active",
        "salience": "high",
        "follow_up": "Start with comfort.",
        "context": "Low mood chat.",
        "source_type": "chat",
        "is_sensitive": True,
    }
    assert "sensitive_scope" not in item
