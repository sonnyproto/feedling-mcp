from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402

from enclave import attestation, keys, state  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_sk_cache(monkeypatch):
    monkeypatch.setattr(keys, "_cached_content_sk", None)


def test_dev_seed_derivation_deterministic(monkeypatch):
    monkeypatch.setenv("FEEDLING_DEV_DSTACK_SEED", "seed-a")
    k1 = keys.derive_keys_from_dev_seed()
    k2 = keys.derive_keys_from_dev_seed()
    assert k1["content_pk_bytes"] == k2["content_pk_bytes"]
    assert len(k1["content_pk_bytes"]) == 32
    monkeypatch.setenv("FEEDLING_DEV_DSTACK_SEED", "seed-b")
    assert keys.derive_keys_from_dev_seed()["content_pk_bytes"] != k1["content_pk_bytes"]


def test_get_content_sk_async_uses_dev_seed(monkeypatch):
    monkeypatch.setenv("FEEDLING_DEV_DSTACK_SEED", "seed-a")
    sk = asyncio.run(keys.get_content_sk())
    assert bytes(sk) == bytes(keys.derive_keys_from_dev_seed()["content_sk"])
    # 第二次拿到缓存的同一对象（不重派生）
    assert asyncio.run(keys.get_content_sk()) is sk


def test_report_data_layout():
    pk = b"\x01" * 32
    rd = attestation.build_report_data(
        content_pk_bytes=pk,
        tls_cert_fingerprint=attestation.PHASE1_TLS_FINGERPRINT,
        version_tag=b"feedling-v1",
    )
    assert len(rd) == 64
    assert rd[32:33] == b"\x01"          # version byte
    assert rd[33:34] == b"\x01"          # flag: placeholder fingerprint
    assert rd[34:] == b"\x00" * 30
    with pytest.raises(ValueError):
        attestation.build_report_data(pk, b"\x00" * 31, b"feedling-v1")


def test_bootstrap_dev_seed_populates_state(monkeypatch):
    monkeypatch.setenv("FEEDLING_DEV_DSTACK_SEED", "seed-boot")
    monkeypatch.setitem(state._state, "ready", False)
    state.bootstrap()
    assert state._state["ready"] is True
    assert state._state["error"] is None
    assert len(state._state["content_pk_hex"]) == 64
    att = state._state["attestation"]
    assert att["app_id"] == "dev-memory-sandbox"
