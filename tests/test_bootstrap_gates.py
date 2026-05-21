"""
Bootstrap-stage gate tests.

Covers two failure modes that hit prod 2026-05-13..15:

1. /v1/bootstrap/status counted agent messages by role=="agent" but
   /v1/chat/response writes role="openclaw" → agent_messages_count
   stuck at 0 even with chat traffic. Fix at backend/app.py:2362,
   :2394 (accept both roles). Tested below in
   test_bootstrap_status_counts_openclaw_role.

2. Agent runtimes (OpenClaw specifically) skipping Pass 1-3 + Step 5
   and going straight to chat_post — server would happily accept the
   writes even though the agent had hallucinated bootstrap completion.
   Fix at backend/app.py: /v1/identity/init and /v1/chat/response now
   return 409 bootstrap_incomplete unless prerequisites are satisfied.
   Tested below.

The fixture spawns a fresh Flask backend in a subprocess against a temp
data dir, identical to test_multi_tenant_isolation.py. Hermetic — does
not touch prod.
"""

from __future__ import annotations

import base64
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
TIMEOUT = 8


# ---------------------------------------------------------------------------
# Fixture (mirrors test_multi_tenant_isolation.py)
# ---------------------------------------------------------------------------

def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def backend():
    port = _pick_free_port()
    ws_port = _pick_free_port()
    tmp_data = tempfile.mkdtemp(prefix="feedling-gate-test-")
    env = {
        **os.environ,
        "FEEDLING_DATA_DIR": tmp_data,
        "FEEDLING_WS_PORT": str(ws_port),
        "FEEDLING_PORT": str(port),
        "PORT": str(port),
    }
    log_path = Path(tmp_data) / "backend.log"
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-u", str(BACKEND_DIR / "app.py")],
        env=env, stdout=log, stderr=subprocess.STDOUT, cwd=str(BACKEND_DIR),
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            r = requests.get(f"{base_url}/healthz", timeout=1)
            if r.status_code == 200:
                break
        except Exception:
            pass
        if proc.poll() is not None:
            log.close()
            raise RuntimeError(f"backend died early; log:\n{log_path.read_text()}")
        time.sleep(0.2)
    else:
        proc.kill()
        log.close()
        raise RuntimeError(f"backend never came up; log:\n{log_path.read_text()}")

    yield {"base_url": base_url, "data_dir": tmp_data}

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _stub_envelope(owner_uid: str, marker: str) -> dict:
    payload = f"{owner_uid}|{marker}".encode("utf-8")
    return {
        "v": 1,
        "id": uuid.uuid4().hex,
        "body_ct": _b64(payload),
        "nonce": _b64(b"\x00" * 12),
        "K_user": _b64(b"\x00" * 32),
        "K_enclave": _b64(b"\x00" * 32),
        "visibility": "shared",
        "owner_user_id": owner_uid,
    }


def _register(base_url: str) -> tuple[str, str]:
    r = requests.post(f"{base_url}/v1/users/register", json={}, timeout=TIMEOUT)
    assert r.status_code == 201, f"register failed: {r.text}"
    body = r.json()
    return body["user_id"], body["api_key"]


def _add_memory(
    base_url: str,
    user_id: str,
    api_key: str,
    marker: str,
    occurred_at: str | None = None,
) -> None:
    """Write one memory. Default occurred_at = now → relationship_age=0
    → tier <2 days → floor=1. Tests that exercise specific age tiers
    should pass occurred_at explicitly.
    """
    env = _stub_envelope(user_id, marker)
    env["occurred_at"] = occurred_at or datetime.now().isoformat()
    r = requests.post(
        f"{base_url}/v1/memory/add",
        json={"envelope": env},
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    assert r.status_code in (200, 201), f"memory_add failed: {r.text}"


def _init_identity(base_url: str, user_id: str, api_key: str, days: int = 0) -> requests.Response:
    env = _stub_envelope(user_id, "identity")
    return requests.post(
        f"{base_url}/v1/identity/init",
        json={"envelope": env, "days_with_user": days},
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )


def _chat_response(base_url: str, user_id: str, api_key: str) -> requests.Response:
    env = _stub_envelope(user_id, "chat-reply")
    return requests.post(
        f"{base_url}/v1/chat/response",
        json={"envelope": env, "alert_body": "hi"},
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )


# ---------------------------------------------------------------------------
# P1: bootstrap gates — /v1/chat/response and /v1/identity/init refuse
# writes when prerequisites aren't satisfied.
# ---------------------------------------------------------------------------

def test_chat_response_blocked_when_no_memory_no_identity(backend):
    """Fresh user — chat_response must 409 with bootstrap_incomplete /
    stage=needs_memory and the actionable instructions in `required`."""
    user_id, api_key = _register(backend["base_url"])
    r = _chat_response(backend["base_url"], user_id, api_key)
    assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"
    body = r.json()
    assert body["error"] == "bootstrap_incomplete"
    assert body["stage"] == "needs_memory"
    assert body["memory_count"] == 0
    assert body["memory_floor"] >= 1
    assert "feedling_memory_add_moment" in body["required"]
    assert "skill_url" in body


def test_chat_response_blocked_when_memory_ok_but_no_identity(backend):
    """User wrote enough memories but never initialized identity — chat
    still 409s with stage=needs_identity."""
    user_id, api_key = _register(backend["base_url"])
    for i in range(3):
        _add_memory(backend["base_url"], user_id, api_key, f"m{i}")
    r = _chat_response(backend["base_url"], user_id, api_key)
    assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"
    body = r.json()
    assert body["error"] == "bootstrap_incomplete"
    assert body["stage"] == "needs_identity"
    assert body["memory_count"] >= 3
    assert body["identity_written"] is False


def test_chat_response_allowed_after_full_bootstrap(backend):
    """3 memories + identity_init → chat_response 200."""
    user_id, api_key = _register(backend["base_url"])
    for i in range(3):
        _add_memory(backend["base_url"], user_id, api_key, f"m{i}")
    r = _init_identity(backend["base_url"], user_id, api_key)
    assert r.status_code == 201, f"identity_init failed: {r.text}"
    r = _chat_response(backend["base_url"], user_id, api_key)
    assert r.status_code == 200, f"chat_response should succeed: {r.text}"


def test_identity_init_blocked_when_no_memory(backend):
    """Identity must be DERIVED from memories. With zero memories the
    agent is making things up — refuse the write."""
    user_id, api_key = _register(backend["base_url"])
    r = _init_identity(backend["base_url"], user_id, api_key)
    assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"
    body = r.json()
    assert body["error"] == "bootstrap_incomplete"
    assert body["stage"] == "needs_memory"


def test_identity_init_allowed_after_3_memories(backend):
    user_id, api_key = _register(backend["base_url"])
    for i in range(3):
        _add_memory(backend["base_url"], user_id, api_key, f"m{i}")
    r = _init_identity(backend["base_url"], user_id, api_key)
    assert r.status_code == 201, f"identity_init should succeed: {r.text}"


def test_identity_init_blocked_when_below_age_tier_floor(backend):
    """Floor is per-age. For a ≥1-month relationship the floor is 15;
    writing 2 cards trips the gate. Older `occurred_at` puts the user
    into the higher-floor tier."""
    user_id, api_key = _register(backend["base_url"])
    two_months_ago = (datetime.now() - timedelta(days=60)).isoformat()
    for i in range(2):
        _add_memory(backend["base_url"], user_id, api_key, f"m{i}",
                    occurred_at=two_months_ago)
    r = _init_identity(backend["base_url"], user_id, api_key)
    assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"
    body = r.json()
    assert body["memory_count"] == 2
    assert body["memory_floor"] >= 15, f"expected ≥1-month floor, got {body}"


def test_identity_init_allowed_with_one_card_for_we_just_met(backend):
    """The <2-days tier needs only 1 card. 'We just met today' is a valid
    bootstrap path — agent and user only have one shared moment so far."""
    user_id, api_key = _register(backend["base_url"])
    # occurred_at = today → relationship_age = 0 → floor = 1
    _add_memory(backend["base_url"], user_id, api_key, "m0")
    r = _init_identity(backend["base_url"], user_id, api_key, days=0)
    assert r.status_code == 201, f"identity_init should succeed: {r.text}"


# ---------------------------------------------------------------------------
# P0: /v1/bootstrap/status must count openclaw-role messages
# (regression for the bug where role=="agent" filter never matched).
# ---------------------------------------------------------------------------

def test_bootstrap_status_counts_openclaw_role(backend):
    """After a successful /v1/chat/response write (which stamps
    role="openclaw"), /v1/bootstrap/status must reflect
    agent_messages_count >= 1, not 0."""
    user_id, api_key = _register(backend["base_url"])
    for i in range(3):
        _add_memory(backend["base_url"], user_id, api_key, f"m{i}")
    assert _init_identity(backend["base_url"], user_id, api_key).status_code == 201
    assert _chat_response(backend["base_url"], user_id, api_key).status_code == 200

    r = requests.get(
        f"{backend['base_url']}/v1/bootstrap/status",
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["agent_messages_count"] >= 1, (
        f"bootstrap_status agent_messages_count stuck at 0 despite a "
        f"successful chat_response — role filter is broken. Full body: {body}"
    )
    assert body["identity_written"] is True
    assert body["memories_count"] >= 3


def test_bootstrap_status_chat_loop_verified_with_openclaw(backend):
    """chat_loop_verified flips true when an openclaw-role reply comes
    AFTER a user message. Earlier the loop body filtered role=="agent"
    only, so this was permanently false even with real loop traffic."""
    user_id, api_key = _register(backend["base_url"])
    for i in range(3):
        _add_memory(backend["base_url"], user_id, api_key, f"m{i}")
    assert _init_identity(backend["base_url"], user_id, api_key).status_code == 201

    # User → agent → user → agent sequence
    user_env = _stub_envelope(user_id, "user-msg")
    r = requests.post(
        f"{backend['base_url']}/v1/chat/message",
        json={"envelope": user_env},
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, f"user chat_message failed: {r.text}"

    # Agent reply (role=openclaw on the server side)
    assert _chat_response(backend["base_url"], user_id, api_key).status_code == 200

    r = requests.get(
        f"{backend['base_url']}/v1/bootstrap/status",
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    body = r.json()
    assert body["chat_loop_verified"] is True, (
        f"chat_loop_verified stuck false despite a user→agent exchange. "
        f"Full body: {body}"
    )
    assert body["is_complete"] is True


def test_bootstrap_status_complete_field_includes_loop_verified(backend):
    """is_complete should be true only when everything is satisfied AND
    chat_loop_verified is true (post-greeting greetings alone don't count
    as a working loop)."""
    user_id, api_key = _register(backend["base_url"])
    for i in range(3):
        _add_memory(backend["base_url"], user_id, api_key, f"m{i}")
    assert _init_identity(backend["base_url"], user_id, api_key).status_code == 201
    # Agent posts a greeting but no user message → loop not verified yet
    assert _chat_response(backend["base_url"], user_id, api_key).status_code == 200

    r = requests.get(
        f"{backend['base_url']}/v1/bootstrap/status",
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    body = r.json()
    assert body["agent_messages_count"] >= 1
    assert body["chat_loop_verified"] is False
    assert body["is_complete"] is False


# ---------------------------------------------------------------------------
# Phase 2: Verify endpoints — memory_verify / identity_verify /
# chat_verify_loop. Surface QUALITY signals on top of the existing GATES.
# ---------------------------------------------------------------------------

def test_memory_verify_empty_user(backend):
    user_id, api_key = _register(backend["base_url"])
    r = requests.get(
        f"{backend['base_url']}/v1/memory/verify",
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 0
    assert body["below_floor"] is True
    assert body["passing"] is False
    assert len(body["suggestions"]) >= 1


def test_memory_verify_passing_at_relationship_floor(backend):
    """User with 5 memories in the 2-30 day tier → floor 5. Hitting the
    floor is passing."""
    user_id, api_key = _register(backend["base_url"])
    # 10 days ago → 2-30 day tier → floor = 5
    ten_days_ago = (datetime.now() - timedelta(days=10)).isoformat()
    for i in range(5):
        _add_memory(backend["base_url"], user_id, api_key, f"m{i}",
                    occurred_at=ten_days_ago)
    r = requests.get(
        f"{backend['base_url']}/v1/memory/verify",
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    body = r.json()
    assert body["count"] == 5
    assert body["floor"] == 5, f"unexpected floor for 2-30d tier: {body}"
    assert body["below_floor"] is False
    assert body["passing"] is True


def test_memory_verify_floor_for_we_just_met(backend):
    """The <2-days tier has floor=1. A single card written today should
    pass verify."""
    user_id, api_key = _register(backend["base_url"])
    _add_memory(backend["base_url"], user_id, api_key, "m0")
    r = requests.get(
        f"{backend['base_url']}/v1/memory/verify",
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    body = r.json()
    assert body["count"] == 1
    assert body["floor"] == 1, f"expected <2d floor=1, got {body}"
    assert body["passing"] is True


def test_identity_verify_not_written(backend):
    user_id, api_key = _register(backend["base_url"])
    r = requests.get(
        f"{backend['base_url']}/v1/identity/verify",
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    body = r.json()
    assert body["written"] is False
    assert body["passing"] is False


def test_identity_verify_after_init(backend):
    user_id, api_key = _register(backend["base_url"])
    for i in range(3):
        _add_memory(backend["base_url"], user_id, api_key, f"m{i}")
    _init_identity(backend["base_url"], user_id, api_key)
    r = requests.get(
        f"{backend['base_url']}/v1/identity/verify",
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    body = r.json()
    assert body["written"] is True
    assert body["relationship_anchored"] is True
    assert body["passing"] is True


def test_chat_verify_loop_no_agent_returns_dead(backend):
    """No agent connected → synthetic ping times out → passing=false +
    suggestions guide operator to run feedling-chat-resident."""
    user_id, api_key = _register(backend["base_url"])
    r = requests.post(
        f"{backend['base_url']}/v1/chat/verify_loop",
        json={"timeout_sec": 4},  # short timeout for test speed
        headers={"X-API-Key": api_key},
        timeout=10,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["loop_alive"] is False
    assert body["passing"] is False
    assert body["response_time_sec"] is None
    assert len(body["suggestions"]) >= 1
    assert "feedling-chat-resident" in body["suggestions"][0]


def test_chat_verify_loop_synthetic_ping_does_not_pollute_history(backend):
    """The synthetic ping must NOT leave a __VERIFY_PING__ marker in the
    user's actual chat history after the verify completes."""
    user_id, api_key = _register(backend["base_url"])
    # Add some user chat messages first so history isn't empty
    env = _stub_envelope(user_id, "hello")
    requests.post(
        f"{backend['base_url']}/v1/chat/message",
        json={"envelope": env},
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    requests.post(
        f"{backend['base_url']}/v1/chat/verify_loop",
        json={"timeout_sec": 3},
        headers={"X-API-Key": api_key},
        timeout=10,
    )
    # After verify completes, the synthetic message must be GC'd
    r = requests.get(
        f"{backend['base_url']}/v1/chat/history?limit=50",
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    msgs = r.json().get("messages", [])
    for m in msgs:
        assert m.get("source") != "verify_ping", \
            f"synthetic ping leaked into user history: {m}"


def test_chat_verify_loop_marks_live_connection_without_first_message(backend):
    """A successful synthetic verify marks Live connection in bootstrap
    status, but the private verify reply must not count as the visible
    first message that opens Chat."""
    user_id, api_key = _register(backend["base_url"])
    for i in range(3):
        _add_memory(backend["base_url"], user_id, api_key, f"m{i}")
    assert _init_identity(backend["base_url"], user_id, api_key).status_code == 201

    def delayed_agent_reply():
        time.sleep(0.5)
        _chat_response(backend["base_url"], user_id, api_key)

    t = threading.Thread(target=delayed_agent_reply)
    t.start()
    r = requests.post(
        f"{backend['base_url']}/v1/chat/verify_loop",
        json={"timeout_sec": 6},
        headers={"X-API-Key": api_key},
        timeout=10,
    )
    t.join(timeout=5)
    assert r.status_code == 200
    body = r.json()
    assert body["passing"] is True

    r = requests.get(
        f"{backend['base_url']}/v1/bootstrap/status",
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    status = r.json()
    assert status["chat_loop_verified"] is True
    assert status["agent_messages_count"] == 0
    assert status["is_complete"] is False
