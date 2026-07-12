"""Task 4: `/v1/identity/init` (plaintext branch) must reject a structurally
bad card via `card_policy.validate_full_identity_card` (contract B) — a
runtime-label agent_name is rejected, but a sparse (2-dimension) card still
succeeds. Mirrors the fixture/setup pattern in
`test_identity_init_server_encrypt.py`.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from core import store as core_store  # noqa: E402


ANCHOR = "transcript: earliest chat dated 2026-05-08"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    with make_client() as c:
        yield c


def _register(client) -> tuple[str, str]:
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def _fake_envelope_builder(captured: list):
    def _build(store, plaintext: bytes, item_id: str | None = None):
        captured.append(json.loads(plaintext.decode("utf-8")))
        envelope = {
            "id": item_id or "env_1",
            "body_ct": "ct_1",
            "nonce": "nonce_1",
            "K_user": "k_user_1",
            "K_enclave": "k_enclave_1",
            "visibility": "shared",
            "owner_user_id": store.user_id,
            "enclave_pk_fpr": "test",
        }
        return envelope, ""
    return _build


def test_init_rejects_runtime_label_name(client, monkeypatch):
    _user_id, api_key = _register(client)
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder([]))
    body = {
        "identity": {"agent_name": "Claude",
                     "dimensions": [{"name": "锐利", "value": 90, "description": "x"}]},
        "days_with_user": 3,
        "relationship_anchor_evidence": ANCHOR,
    }
    resp = client.post("/v1/identity/init", headers=_headers(api_key), json=body)
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "agent_name_is_runtime_label"


def test_init_accepts_sparse_two_dimensions(client, monkeypatch):
    _user_id, api_key = _register(client)
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder([]))
    body = {
        "identity": {"agent_name": "阿锐",
                     "dimensions": [{"name": "锐利", "value": 90, "description": "x"},
                                    {"name": "直接", "value": 88, "description": "y"}]},
        "days_with_user": 3,
        "relationship_anchor_evidence": ANCHOR,
    }
    resp = client.post("/v1/identity/init", headers=_headers(api_key), json=body)
    assert resp.status_code == 201  # 契约 B:稀疏合法


def test_init_rejects_out_of_range_dimension_value(client, monkeypatch):
    _user_id, api_key = _register(client)
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder([]))
    body = {
        "identity": {"agent_name": "阿锐",
                     "dimensions": [{"name": "锐利", "value": 150, "description": "x"}]},
        "days_with_user": 3,
        "relationship_anchor_evidence": ANCHOR,
    }
    resp = client.post("/v1/identity/init", headers=_headers(api_key), json=body)
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "dimension_value_out_of_range"


def test_init_prebuilt_envelope_path_untouched(client):
    """Envelope-direct path must NOT run card_policy — a runtime-label-shaped
    plaintext trapped inside an opaque ciphertext envelope is invisible to the
    server by design, and this call must still succeed."""
    user_id, api_key = _register(client)
    res = client.post(
        "/v1/identity/init",
        headers=_headers(api_key),
        json={
            "envelope": {
                "id": "client_env_1",
                "body_ct": "client_ct",
                "nonce": "client_nonce",
                "K_user": "client_k_user",
                "K_enclave": "client_k_enclave",
                "visibility": "shared",
                "owner_user_id": user_id,
                "enclave_pk_fpr": "client",
            },
            "days_with_user": 10,
            "relationship_anchor_evidence": ANCHOR,
        },
    )
    assert res.status_code == 201, res.get_data(as_text=True)
