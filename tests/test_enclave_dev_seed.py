from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from enclave import state as enclave_state  # noqa: E402


def test_enclave_bootstrap_can_use_dev_seed_without_dstack(monkeypatch):
    monkeypatch.setenv("FEEDLING_DEV_DSTACK_SEED", "memory-sandbox-test-seed")
    monkeypatch.setenv("FEEDLING_ENCLAVE_TLS", "false")

    class ExplodingDstackClient:
        def __init__(self):
            raise AssertionError("DstackClient should not be constructed in dev seed mode")

    monkeypatch.setattr(enclave_state, "DstackClient", ExplodingDstackClient)
    monkeypatch.setitem(enclave_state._state, "ready", False)
    monkeypatch.setitem(enclave_state._state, "error", None)
    enclave_state.bootstrap()

    assert enclave_state._state["ready"] is True
    assert len(enclave_state._state["content_pk_hex"]) == 64
    assert enclave_state._state["attestation"]["compose_hash"].startswith("dev-memory-sandbox-")
    assert enclave_state._state["attestation"]["measurements"]["mrtd"] == "00" * 48
