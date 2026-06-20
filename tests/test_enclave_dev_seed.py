from __future__ import annotations

import importlib
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))


def test_enclave_bootstrap_can_use_dev_seed_without_dstack(monkeypatch):
    monkeypatch.setenv("FEEDLING_DEV_DSTACK_SEED", "memory-sandbox-test-seed")
    monkeypatch.setenv("FEEDLING_ENCLAVE_TLS", "false")
    import enclave_app

    enclave_app = importlib.reload(enclave_app)

    class ExplodingDstackClient:
        def __init__(self):
            raise AssertionError("DstackClient should not be constructed in dev seed mode")

    monkeypatch.setattr(enclave_app, "DstackClient", ExplodingDstackClient)
    enclave_app.bootstrap()

    assert enclave_app._state["ready"] is True
    assert len(enclave_app._state["content_pk_hex"]) == 64
    assert enclave_app._state["attestation"]["compose_hash"].startswith("dev-memory-sandbox-")
    assert enclave_app._state["attestation"]["measurements"]["mrtd"] == "00" * 48
