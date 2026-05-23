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
    mem_type: str = "fact",
    anchor_memory_ids: list[str] | None = None,
    expect_status: tuple[int, ...] = (200, 201),
) -> dict:
    """Write one memory. Default type='fact' (About me tab) — most tests
    want cards that quickly fill the density floor; fact has the lowest
    description threshold and no anchor requirements.

    Default occurred_at = now → relationship_age = 0 → tier <2 days →
    story=1, about_me=1 floor. Tests that exercise specific age tiers
    should pass occurred_at explicitly.

    Returns the response body (`{status, moment, v}`) on success so
    callers can chain the returned moment id (needed for insight /
    reflection anchor tests).
    """
    env = _stub_envelope(user_id, marker)
    env["occurred_at"] = occurred_at or datetime.now().isoformat()
    env["type"] = mem_type
    if anchor_memory_ids:
        env["anchor_memory_ids"] = list(anchor_memory_ids)
    r = requests.post(
        f"{base_url}/v1/memory/add",
        json={"envelope": env},
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    assert r.status_code in expect_status, (
        f"memory_add type={mem_type} expected {expect_status}, "
        f"got {r.status_code}: {r.text}"
    )
    return r.json()


def _seed_passing_bootstrap(base_url: str, user_id: str, api_key: str,
                             occurred_at: str | None = None) -> None:
    """Write the minimum cards to satisfy the <2-day per-tab floors:
    1 moment (Story) + 1 fact (About me). Used by tests that want the
    bootstrap gate open so they can exercise downstream behavior.
    """
    _add_memory(base_url, user_id, api_key, "story-0",
                occurred_at=occurred_at, mem_type="moment")
    _add_memory(base_url, user_id, api_key, "fact-0",
                occurred_at=occurred_at, mem_type="fact")


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
    assert body["counts"]["total"] == 0
    assert "story" in body["missing_tabs"] and "about_me" in body["missing_tabs"]
    assert "feedling_memory_add_moment" in body["required"]
    assert "skill_url" in body


def test_chat_response_blocked_when_memory_ok_but_no_identity(backend):
    """User wrote enough memories (Story + About me floor met) but never
    initialized identity — chat still 409s with stage=needs_identity."""
    user_id, api_key = _register(backend["base_url"])
    _seed_passing_bootstrap(backend["base_url"], user_id, api_key)
    r = _chat_response(backend["base_url"], user_id, api_key)
    assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"
    body = r.json()
    assert body["error"] == "bootstrap_incomplete"
    assert body["stage"] == "needs_identity"
    assert body["memory_count"] >= 2
    assert body["identity_written"] is False


def test_chat_response_allowed_after_full_bootstrap(backend):
    """Story floor + About me floor + identity_init → chat_response 200."""
    user_id, api_key = _register(backend["base_url"])
    _seed_passing_bootstrap(backend["base_url"], user_id, api_key)
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
    assert "story" in body["missing_tabs"]
    assert "about_me" in body["missing_tabs"]


def test_identity_init_blocked_when_only_story_filled(backend):
    """About me tab is the density layer. Writing only moments/quotes
    (Story) without facts/events (About me) still 409s — the floor is
    per-tab, not total."""
    user_id, api_key = _register(backend["base_url"])
    # Fill Story tab generously, but About me stays empty.
    for i in range(5):
        _add_memory(backend["base_url"], user_id, api_key, f"m{i}",
                    mem_type="moment")
    r = _init_identity(backend["base_url"], user_id, api_key)
    assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"
    body = r.json()
    assert "about_me" in body["missing_tabs"]
    assert "story" not in body["missing_tabs"]


def test_identity_init_blocked_when_only_about_me_filled(backend):
    """Mirror case: filling only facts/events without any moments/quotes
    also blocks. Identity needs both layers."""
    user_id, api_key = _register(backend["base_url"])
    for i in range(5):
        _add_memory(backend["base_url"], user_id, api_key, f"f{i}",
                    mem_type="fact")
    r = _init_identity(backend["base_url"], user_id, api_key)
    assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"
    body = r.json()
    assert "story" in body["missing_tabs"]
    assert "about_me" not in body["missing_tabs"]


def test_identity_init_allowed_after_minimum_bootstrap(backend):
    user_id, api_key = _register(backend["base_url"])
    _seed_passing_bootstrap(backend["base_url"], user_id, api_key)
    r = _init_identity(backend["base_url"], user_id, api_key)
    assert r.status_code == 201, f"identity_init should succeed: {r.text}"


def test_identity_init_blocked_when_below_age_tier_floor(backend):
    """Floor is per-age. For a ≥1-month relationship the floors jump:
    Story=8, About me=25. Writing 1 moment + 1 fact (which would pass
    the <2-day floor) trips the gate at the higher tier."""
    user_id, api_key = _register(backend["base_url"])
    two_months_ago = (datetime.now() - timedelta(days=60)).isoformat()
    _add_memory(backend["base_url"], user_id, api_key, "m0",
                mem_type="moment", occurred_at=two_months_ago)
    _add_memory(backend["base_url"], user_id, api_key, "f0",
                mem_type="fact", occurred_at=two_months_ago)
    r = _init_identity(backend["base_url"], user_id, api_key)
    assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"
    body = r.json()
    assert body["counts"]["total"] == 2
    assert body["floors"]["story"] >= 8, f"expected ≥1-month story floor, got {body}"
    assert body["floors"]["about_me"] >= 25, f"expected ≥1-month about_me floor, got {body}"


def test_identity_init_allowed_with_one_card_for_we_just_met(backend):
    """The <2-days tier needs only Story=1 + About me=1 (total 2).
    'We just met today' is a valid bootstrap path."""
    user_id, api_key = _register(backend["base_url"])
    _seed_passing_bootstrap(backend["base_url"], user_id, api_key)
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
    _seed_passing_bootstrap(backend["base_url"], user_id, api_key)
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
    assert body["memories_count"] >= 2


def test_bootstrap_status_chat_loop_verified_with_openclaw(backend):
    """chat_loop_verified flips true when an openclaw-role reply comes
    AFTER a user message. Earlier the loop body filtered role=="agent"
    only, so this was permanently false even with real loop traffic."""
    user_id, api_key = _register(backend["base_url"])
    _seed_passing_bootstrap(backend["base_url"], user_id, api_key)
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
    _seed_passing_bootstrap(backend["base_url"], user_id, api_key)
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
    assert body["counts"]["total"] == 0
    assert body["below_floor"]["story"] is True
    assert body["below_floor"]["about_me"] is True
    assert body["passing"] is False
    assert body["passing_full"] is False
    assert len(body["suggestions"]) >= 2  # one per missing tab


def test_memory_verify_passing_at_minimum_floor(backend):
    """<2d tier needs Story=1 + About me=1 (TA Thinking=0 OK).
    Writing 1 moment + 1 fact today should mark passing=true."""
    user_id, api_key = _register(backend["base_url"])
    _seed_passing_bootstrap(backend["base_url"], user_id, api_key)
    r = requests.get(
        f"{backend['base_url']}/v1/memory/verify",
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    body = r.json()
    assert body["counts"]["story"] == 1
    assert body["counts"]["about_me"] == 1
    assert body["floors"]["story"] == 1
    assert body["floors"]["about_me"] == 1
    assert body["passing"] is True  # Story + About me OK
    # TA Thinking floor at <2d is 0, so passing_full also holds.
    assert body["passing_full"] is True


def test_memory_verify_about_me_floor_only_passing(backend):
    """Filling Story + About me but no TA Thinking memories → passing=true
    (identity_init gate uses Story + About me) but passing_full=false."""
    user_id, api_key = _register(backend["base_url"])
    # 10 days ago → 2-30d tier → floors: story=3 / about_me=8 / ta_thinking=2
    ten_days_ago = (datetime.now() - timedelta(days=10)).isoformat()
    for i in range(3):
        _add_memory(backend["base_url"], user_id, api_key, f"m{i}",
                    mem_type="moment", occurred_at=ten_days_ago)
    for i in range(8):
        _add_memory(backend["base_url"], user_id, api_key, f"f{i}",
                    mem_type="fact", occurred_at=ten_days_ago)
    r = requests.get(
        f"{backend['base_url']}/v1/memory/verify",
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    body = r.json()
    assert body["passing"] is True, body
    assert body["passing_full"] is False, body
    assert body["below_floor"]["ta_thinking"] is True


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
    _seed_passing_bootstrap(backend["base_url"], user_id, api_key)
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
    _seed_passing_bootstrap(backend["base_url"], user_id, api_key)
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


# ---------------------------------------------------------------------------
# Phase 3: Typed memory contract (post-2026-05-22 Friend-Test → density rewrite)
#
# - `type` is required plaintext metadata on every memory write
# - insight requires anchor_memory_ids ≥1
# - reflection requires anchor_memory_ids ≥2 + time-cap by relationship age
# - retype tool waives time-cap but enforces substrate gate
# - per-tab counts surface in verify
# ---------------------------------------------------------------------------

def test_memory_add_rejects_missing_type(backend):
    """type is mandatory plaintext metadata. Server must 400 with the
    enum spelled out so the agent can self-correct."""
    user_id, api_key = _register(backend["base_url"])
    env = _stub_envelope(user_id, "no-type")
    env["occurred_at"] = datetime.now().isoformat()
    # NB: deliberately not setting env["type"]
    r = requests.post(
        f"{backend['base_url']}/v1/memory/add",
        json={"envelope": env},
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "type_required"
    assert set(body["allowed"]) == {
        "moment", "quote", "fact", "event", "insight", "reflection",
    }


def test_memory_add_rejects_invalid_type(backend):
    user_id, api_key = _register(backend["base_url"])
    env = _stub_envelope(user_id, "bad-type")
    env["occurred_at"] = datetime.now().isoformat()
    env["type"] = "SHARED_GROWTH"  # legacy free-form garbage
    r = requests.post(
        f"{backend['base_url']}/v1/memory/add",
        json={"envelope": env},
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    assert r.status_code == 400
    assert r.json()["error"] == "type_invalid"


def test_memory_add_insight_requires_anchor(backend):
    """insight without anchor_memory_ids → 400 with actionable hint."""
    user_id, api_key = _register(backend["base_url"])
    env = _stub_envelope(user_id, "naked-insight")
    env["occurred_at"] = datetime.now().isoformat()
    env["type"] = "insight"
    r = requests.post(
        f"{backend['base_url']}/v1/memory/add",
        json={"envelope": env},
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    assert r.status_code == 400
    assert r.json()["error"] == "insight_requires_anchor"


def test_memory_add_insight_with_anchor_succeeds(backend):
    """insight pointing at an existing memory → 201."""
    user_id, api_key = _register(backend["base_url"])
    fact = _add_memory(backend["base_url"], user_id, api_key, "f1", mem_type="fact")
    insight = _add_memory(
        backend["base_url"], user_id, api_key, "ins1",
        mem_type="insight",
        anchor_memory_ids=[fact["moment"]["id"]],
    )
    assert insight["moment"]["type"] == "insight"
    assert insight["moment"]["anchor_memory_ids"] == [fact["moment"]["id"]]


def test_memory_add_reflection_requires_two_anchors(backend):
    """reflection with 1 anchor → 400 (substrate gate)."""
    user_id, api_key = _register(backend["base_url"])
    fact = _add_memory(backend["base_url"], user_id, api_key, "f1", mem_type="fact")
    env = _stub_envelope(user_id, "thin-reflection")
    env["occurred_at"] = datetime.now().isoformat()
    env["type"] = "reflection"
    env["anchor_memory_ids"] = [fact["moment"]["id"]]
    r = requests.post(
        f"{backend['base_url']}/v1/memory/add",
        json={"envelope": env},
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    assert r.status_code == 400
    assert r.json()["error"] == "reflection_requires_substrate"


def test_memory_add_reflection_anchor_must_exist(backend):
    """anchor_memory_ids referencing a non-existent id → 400."""
    user_id, api_key = _register(backend["base_url"])
    f1 = _add_memory(backend["base_url"], user_id, api_key, "f1", mem_type="fact")
    env = _stub_envelope(user_id, "fake-anchor")
    env["occurred_at"] = datetime.now().isoformat()
    env["type"] = "reflection"
    env["anchor_memory_ids"] = [f1["moment"]["id"], "mom_nonexistent"]
    r = requests.post(
        f"{backend['base_url']}/v1/memory/add",
        json={"envelope": env},
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "anchor_memory_ids_not_found"
    assert "mom_nonexistent" in body["missing"]


def test_memory_add_reflection_lifetime_cap_under_30d(backend):
    """<30 days relationship → max 2 reflections lifetime. 3rd → 429."""
    user_id, api_key = _register(backend["base_url"])
    # Build substrate at today (relationship_age=0 → <30 days tier).
    f1 = _add_memory(backend["base_url"], user_id, api_key, "f1", mem_type="fact")
    f2 = _add_memory(backend["base_url"], user_id, api_key, "f2", mem_type="fact")
    anchors = [f1["moment"]["id"], f2["moment"]["id"]]

    # Reflections 1 + 2 should pass.
    for i in range(2):
        _add_memory(
            backend["base_url"], user_id, api_key, f"r{i}",
            mem_type="reflection",
            anchor_memory_ids=anchors,
        )

    # 3rd reflection at this tier → 429.
    env = _stub_envelope(user_id, "r3")
    env["occurred_at"] = datetime.now().isoformat()
    env["type"] = "reflection"
    env["anchor_memory_ids"] = anchors
    r = requests.post(
        f"{backend['base_url']}/v1/memory/add",
        json={"envelope": env},
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    assert r.status_code == 429
    assert r.json()["error"] == "reflection_lifetime_cap"


def test_memory_retype_changes_tab(backend):
    """Retype a fact → moment. About me count goes down, Story up."""
    user_id, api_key = _register(backend["base_url"])
    f1 = _add_memory(backend["base_url"], user_id, api_key, "f1", mem_type="fact")
    fact_id = f1["moment"]["id"]

    r = requests.post(
        f"{backend['base_url']}/v1/memory/retype",
        json={"id": fact_id, "type": "moment"},
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["moment"]["type"] == "moment"

    r = requests.get(
        f"{backend['base_url']}/v1/memory/verify",
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    counts = r.json()["counts"]
    assert counts["story"] == 1
    assert counts["about_me"] == 0


def test_memory_retype_into_insight_requires_anchor(backend):
    """Retype fact → insight without anchors → 400."""
    user_id, api_key = _register(backend["base_url"])
    f1 = _add_memory(backend["base_url"], user_id, api_key, "f1", mem_type="fact")
    r = requests.post(
        f"{backend['base_url']}/v1/memory/retype",
        json={"id": f1["moment"]["id"], "type": "insight"},
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    assert r.status_code == 400
    assert r.json()["error"] == "insight_requires_anchor"


def test_memory_retype_self_reference_rejected(backend):
    """A memory cannot anchor itself when retyping."""
    user_id, api_key = _register(backend["base_url"])
    f1 = _add_memory(backend["base_url"], user_id, api_key, "f1", mem_type="fact")
    fid = f1["moment"]["id"]
    r = requests.post(
        f"{backend['base_url']}/v1/memory/retype",
        json={"id": fid, "type": "insight", "anchor_memory_ids": [fid]},
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    assert r.status_code == 400
    assert r.json()["error"] == "anchor_self_reference"


def test_memory_retype_unknown_id_returns_404(backend):
    user_id, api_key = _register(backend["base_url"])
    r = requests.post(
        f"{backend['base_url']}/v1/memory/retype",
        json={"id": "mom_does_not_exist", "type": "fact"},
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    assert r.status_code == 404


def test_per_tab_counts_in_verify(backend):
    """Write one of each non-anchor type, verify per-tab counts match."""
    user_id, api_key = _register(backend["base_url"])
    _add_memory(backend["base_url"], user_id, api_key, "m1", mem_type="moment")
    _add_memory(backend["base_url"], user_id, api_key, "q1", mem_type="quote")
    _add_memory(backend["base_url"], user_id, api_key, "f1", mem_type="fact")
    _add_memory(backend["base_url"], user_id, api_key, "e1", mem_type="event")
    r = requests.get(
        f"{backend['base_url']}/v1/memory/verify",
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    counts = r.json()["counts"]
    assert counts["story"] == 2       # moment + quote
    assert counts["about_me"] == 2    # fact + event
    assert counts["ta_thinking"] == 0
    assert counts["total"] == 4


def test_running_capture_supported_after_main_loop(backend):
    """After full bootstrap + identity_init, writing a new fact mid-session
    must succeed (running capture path — not blocked by any gate)."""
    user_id, api_key = _register(backend["base_url"])
    _seed_passing_bootstrap(backend["base_url"], user_id, api_key)
    assert _init_identity(backend["base_url"], user_id, api_key).status_code == 201
    # Simulate mid-session capture
    captured = _add_memory(
        backend["base_url"], user_id, api_key, "running-fact",
        mem_type="fact",
        # `source` is a free-form plaintext field; running capture should
        # use "chat" / "live_conversation" to mark provenance.
    )
    assert captured["moment"]["type"] == "fact"


# ---------------------------------------------------------------------------
# Phase 4: archive_language defense (Memory Garden language lock layer 2)
#
# Beyond the skill-level "don't switch language" rule, the server now
# stores the user's iOS-system locale at registration and surfaces it
# on /v1/users/whoami, /v1/bootstrap, /v1/memory/verify so the agent
# has an authoritative signal instead of inferring from chat drift.
# ---------------------------------------------------------------------------

def _register_with_lang(base_url: str, archive_language: str | None) -> tuple[str, str]:
    payload: dict = {}
    if archive_language is not None:
        payload["archive_language"] = archive_language
    r = requests.post(f"{base_url}/v1/users/register", json=payload, timeout=TIMEOUT)
    assert r.status_code == 201, r.text
    body = r.json()
    return body["user_id"], body["api_key"]


def test_register_with_archive_language_persists_on_whoami(backend):
    user_id, api_key = _register_with_lang(backend["base_url"], "en")
    r = requests.get(
        f"{backend['base_url']}/v1/users/whoami",
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    assert r.json().get("archive_language") == "en"


def test_register_without_archive_language_returns_no_field(backend):
    user_id, api_key = _register_with_lang(backend["base_url"], None)
    r = requests.get(
        f"{backend['base_url']}/v1/users/whoami",
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    body = r.json()
    # Field is omitted entirely (not null) when unset — easier for clients
    # using strict decoders.
    assert "archive_language" not in body or body["archive_language"] is None


def test_preferences_update_archive_language(backend):
    """Legacy backfill path: account registered without the field, iOS
    posts the locale on next launch."""
    user_id, api_key = _register_with_lang(backend["base_url"], None)
    r = requests.post(
        f"{backend['base_url']}/v1/users/preferences",
        json={"archive_language": "zh-Hans"},
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    assert r.json()["archive_language"] == "zh-Hans"
    # Confirm persistence via whoami round-trip.
    r = requests.get(
        f"{backend['base_url']}/v1/users/whoami",
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    assert r.json()["archive_language"] == "zh-Hans"


def test_preferences_clear_archive_language(backend):
    user_id, api_key = _register_with_lang(backend["base_url"], "ja")
    r = requests.post(
        f"{backend['base_url']}/v1/users/preferences",
        json={"archive_language": None},
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200
    assert r.json()["archive_language"] is None
    r = requests.get(
        f"{backend['base_url']}/v1/users/whoami",
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    assert "archive_language" not in r.json() or r.json()["archive_language"] is None


def test_preferences_rejects_missing_field(backend):
    user_id, api_key = _register_with_lang(backend["base_url"], "en")
    r = requests.post(
        f"{backend['base_url']}/v1/users/preferences",
        json={},  # missing archive_language key entirely
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    assert r.status_code == 400


def test_bootstrap_response_surfaces_archive_language(backend):
    user_id, api_key = _register_with_lang(backend["base_url"], "zh-Hant-TW")
    r = requests.post(
        f"{backend['base_url']}/v1/bootstrap",
        json={}, headers={"X-API-Key": api_key}, timeout=TIMEOUT,
    )
    assert r.status_code == 200
    assert r.json().get("archive_language") == "zh-Hant-TW"


def test_memory_verify_surfaces_archive_language(backend):
    user_id, api_key = _register_with_lang(backend["base_url"], "en")
    r = requests.get(
        f"{backend['base_url']}/v1/memory/verify",
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200
    assert r.json().get("archive_language") == "en"
