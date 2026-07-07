"""Genesis distill routing: the upload path is chosen by BODY TYPE, not a global switch.

A sealed body (self-hosted app/agent encrypted it client-side) → resident lane (the user's
own local agent distills). A plaintext body (cloud app) → the server-side worker. Both lanes
coexist on one backend, so cloud and self-hosted users each get the right path automatically.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402
from genesis import genesis_core  # noqa: E402


# --- sealed-body detection (the routing signal) ---

def test_is_sealed_body_true():
    assert genesis_core._is_sealed_body({"format": "sealed_v1", "envelope": {}}) is True


def test_is_sealed_body_false_legacy():
    assert genesis_core._is_sealed_body({"format": "auto", "content": "hi"}) is False
    assert genesis_core._is_sealed_body({}) is False


# --- routing in plaintext_import ---

def _raise(*a, **k):
    raise RuntimeError("reached_worker")


def _call(payload):
    # A plaintext body proceeds into the injected worker helpers (proven via _raise);
    # a sealed body is routed to _resident_sealed_import BEFORE any helper is touched.
    return genesis_core.plaintext_import(
        object(), payload, api_key=None,
        prepare=_raise, find_reusable=_raise,
        plaintext_mode=lambda p, **k: "add_memory",
        job_metadata=_raise, start_job=_raise,
    )


def test_sealed_body_routes_to_resident_lane():
    # A sealed body reaches _resident_sealed_import (which then validates the envelope
    # itself: here incomplete → 400 sealed_envelope_incomplete, NOT a worker error).
    body, status = _call({"format": "sealed_v1"})
    assert status == 400
    assert body["error"] == "sealed_envelope_incomplete"


def test_plaintext_body_routes_to_worker():
    with pytest.raises(RuntimeError, match="reached_worker"):
        _call({"format": "auto", "content": "hi"})  # plaintext → server-side worker helpers


def test_no_global_mode_switch_remains():
    # The old deploy-level mode function is gone; routing is body-driven only.
    assert not hasattr(genesis_core, "genesis_distill_mode")
