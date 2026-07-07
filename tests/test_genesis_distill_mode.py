"""P1: genesis distill-mode gating + sealed-body schema (bidirectional validation).

The safety edge for the VPS resident-distill feature: worker mode must never ingest
a client-sealed body, and resident mode must never ingest a legacy plaintext body.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402
from genesis import genesis_core  # noqa: E402


# --- Task 1: helpers ---

def test_distill_mode_defaults_worker(monkeypatch):
    monkeypatch.delenv("FEEDLING_GENESIS_DISTILL_MODE", raising=False)
    assert genesis_core.genesis_distill_mode() == "worker"


def test_distill_mode_resident(monkeypatch):
    monkeypatch.setenv("FEEDLING_GENESIS_DISTILL_MODE", "resident")
    assert genesis_core.genesis_distill_mode() == "resident"


def test_distill_mode_garbage_is_worker(monkeypatch):
    monkeypatch.setenv("FEEDLING_GENESIS_DISTILL_MODE", "nonsense")
    assert genesis_core.genesis_distill_mode() == "worker"


def test_is_sealed_body_true():
    assert genesis_core._is_sealed_body({"format": "sealed_v1", "sealed_envelope": {}}) is True


def test_is_sealed_body_false_legacy():
    assert genesis_core._is_sealed_body({"format": "auto", "content": "hi"}) is False
    assert genesis_core._is_sealed_body({}) is False


# --- Task 2: bidirectional gating in plaintext_import ---

def _raise(*a, **k):
    raise RuntimeError("reached_helpers")


def _call(payload):
    # On a reject, gating returns before touching store/helpers. On a pass, it reaches
    # the injected helpers — we prove that via _raise.
    return genesis_core.plaintext_import(
        object(), payload, api_key=None,
        prepare=_raise, find_reusable=_raise,
        plaintext_mode=lambda p, **k: "add_memory",
        job_metadata=_raise, start_job=_raise,
    )


def test_worker_rejects_sealed(monkeypatch):
    monkeypatch.delenv("FEEDLING_GENESIS_DISTILL_MODE", raising=False)  # worker
    body, status = _call({"format": "sealed_v1", "sealed_envelope": {}})
    assert status == 400
    assert body["error"] == "sealed_body_rejected_in_worker_mode"


def test_resident_rejects_plaintext(monkeypatch):
    monkeypatch.setenv("FEEDLING_GENESIS_DISTILL_MODE", "resident")
    body, status = _call({"format": "auto", "content": "hi"})
    assert status == 400
    assert body["error"] == "plaintext_body_rejected_in_resident_mode"


def test_resident_sealed_501_until_p2(monkeypatch):
    monkeypatch.setenv("FEEDLING_GENESIS_DISTILL_MODE", "resident")
    body, status = _call({"format": "sealed_v1", "sealed_envelope": {}})
    assert status == 501
    assert body["error"] == "resident_distill_not_available"


def test_worker_plaintext_proceeds_past_gating(monkeypatch):
    monkeypatch.delenv("FEEDLING_GENESIS_DISTILL_MODE", raising=False)  # worker
    with pytest.raises(RuntimeError, match="reached_helpers"):
        _call({"format": "auto", "content": "hi"})  # passes gating → hits injected helper
