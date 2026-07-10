"""User-configured remote HTTP MCP servers — storage/CRUD core (Task 1).

Exercises ``hosted.mcp_core`` directly against a real per-user store backed
by the test Postgres DB (see conftest.py). ``_build_shared_envelope_for_store``
depends on a reachable enclave for the real key material, so tests stub it
with a deterministic fake — this module only cares that mcp_core round-trips
whatever the envelope builder hands back and never leaks plaintext secrets
into the public (masked) view.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import mcp_core  # noqa: E402
from hosted import mcp_probe as _probe  # noqa: E402


@pytest.fixture()
def store(backend_env):
    user = registry._register_user(public_key="A" * 43 + "=", archive_language="en")
    return core_store.get_store(user["user_id"])


def _fake_envelope(monkeypatch):
    # Envelope construction depends on the enclave being reachable; stub it
    # with a deterministic fake for these unit-level tests.
    from core import envelope as core_envelope
    monkeypatch.setattr(
        core_envelope, "_build_shared_envelope_for_store",
        lambda store, raw, item_id=None: ({"v": 1, "id": item_id, "ct": raw.hex()}, ""),
    )
    # SSRF guard (Task 2) does a real DNS resolve; these tests use *.example.com
    # URLs to exercise mcp_core's own validation only, so stub the network-facing
    # check to always pass — DNS behavior for example.com is environment-dependent
    # (may NXDOMAIN, may be intercepted) and is exercised for real in
    # test_user_mcp_probe.py instead.
    monkeypatch.setattr(_probe, "blocked_url_kind", lambda url: None)


def test_list_empty(store):
    body, status = mcp_core.list_servers(store)
    assert status == 200
    assert body == {"servers": []}
    assert mcp_core.fingerprint_for_store(store) == ""


def test_upsert_and_list_masks_secrets(store, monkeypatch):
    _fake_envelope(monkeypatch)
    body, status = mcp_core.upsert_server(store, {
        "name": "jira",
        "url": "https://mcp.example.com/mcp",
        "headers": {"Authorization": "Bearer secret-token"},
    })
    assert status == 200, body
    body, _ = mcp_core.list_servers(store)
    (srv,) = body["servers"]
    assert srv["name"] == "jira"
    assert srv["url_hint"] == "mcp.example.com"
    assert srv["header_names"] == ["Authorization"]
    assert srv["enabled"] is True
    assert "secret-token" not in str(body)
    assert "config_envelope" not in srv


def test_http_url_rejected(store, monkeypatch):
    _fake_envelope(monkeypatch)
    body, status = mcp_core.upsert_server(store, {
        "name": "x", "url": "http://mcp.example.com", "headers": {}})
    assert status == 400
    assert body["error"]["kind"] == "https_required"


def test_malformed_url_returns_400_not_500(store, monkeypatch):
    # An unterminated IPv6 literal makes urlparse (or .hostname access) raise
    # ValueError; the endpoint must translate that to a clean 400/invalid_url,
    # never let the exception escape as a 500.
    _fake_envelope(monkeypatch)
    body, status = mcp_core.upsert_server(store, {
        "name": "x", "url": "https://[::1", "headers": {}})
    assert status == 400
    assert body["error"]["kind"] == "invalid_url"


def test_upsert_rejects_blocked_url(store, monkeypatch):
    _fake_envelope(monkeypatch)
    # Override the blanket blocked_url_kind -> None stub from _fake_envelope
    # to exercise mcp_core's SSRF-guard wiring itself (Task 2 entry point).
    monkeypatch.setattr(_probe, "blocked_url_kind", lambda url: "blocked_url")
    body, status = mcp_core.upsert_server(store, {
        "name": "x", "url": "https://a.example.com", "headers": {}})
    assert status == 400
    assert body["error"]["kind"] == "blocked_url"


def test_upsert_overwrites_same_name(store, monkeypatch):
    _fake_envelope(monkeypatch)
    first, _ = mcp_core.upsert_server(store, {
        "name": "jira", "url": "https://a.example.com", "headers": {"X-Old": "1"}})
    second, status = mcp_core.upsert_server(store, {
        "name": "jira", "url": "https://b.example.com", "headers": {"X-New": "2"}})
    assert status == 200
    # Same logical server: id and created_at survive the overwrite.
    assert second["id"] == first["id"]
    assert second["created_at"] == first["created_at"]
    assert second["url_hint"] == "b.example.com"
    assert second["header_names"] == ["X-New"]
    body, _ = mcp_core.list_servers(store)
    assert len(body["servers"]) == 1


def test_limits(store, monkeypatch):
    _fake_envelope(monkeypatch)
    for i in range(10):
        _, s = mcp_core.upsert_server(store, {
            "name": f"s{i}", "url": "https://a.example.com", "headers": {}})
        assert s == 200
    body, status = mcp_core.upsert_server(store, {
        "name": "s10", "url": "https://a.example.com", "headers": {}})
    assert status == 400 and body["error"]["kind"] == "too_many_servers"


def test_too_many_headers_rejected(store, monkeypatch):
    _fake_envelope(monkeypatch)
    headers = {f"X-H{i}": "v" for i in range(21)}
    body, status = mcp_core.upsert_server(store, {
        "name": "x", "url": "https://a.example.com", "headers": headers})
    assert status == 400
    assert body["error"]["kind"] == "too_many_headers"


def test_headers_too_large_rejected(store, monkeypatch):
    _fake_envelope(monkeypatch)
    body, status = mcp_core.upsert_server(store, {
        "name": "x", "url": "https://a.example.com",
        "headers": {"X-Big": "a" * 9000}})
    assert status == 400
    assert body["error"]["kind"] == "headers_too_large"


def test_forbidden_host_header_rejected(store, monkeypatch):
    _fake_envelope(monkeypatch)
    body, status = mcp_core.upsert_server(store, {
        "name": "x", "url": "https://a.example.com", "headers": {"Host": "evil.example.com"}})
    assert status == 400
    assert body["error"]["kind"] == "forbidden_header"

    body, status = mcp_core.upsert_server(store, {
        "name": "y", "url": "https://a.example.com", "headers": {"host": "evil.example.com"}})
    assert status == 400
    assert body["error"]["kind"] == "forbidden_header"


def test_bad_name_rejected(store, monkeypatch):
    _fake_envelope(monkeypatch)
    body, status = mcp_core.upsert_server(store, {
        "name": "Not Valid!", "url": "https://a.example.com", "headers": {}})
    assert status == 400
    assert body["error"]["kind"] == "invalid_name"


def test_patch_enabled_keeps_envelope(store, monkeypatch):
    _fake_envelope(monkeypatch)
    mcp_core.upsert_server(store, {"name": "jira", "url": "https://a.example.com", "headers": {}})
    before = mcp_core.envelopes_payload(store)[0]["servers"][0]["config_envelope"]
    fp_before = mcp_core.fingerprint_for_store(store)
    body, status = mcp_core.set_enabled(store, "jira", {"enabled": False})
    assert status == 200 and body["enabled"] is False
    after = mcp_core.envelopes_payload(store)[0]["servers"][0]
    assert after["config_envelope"] == before and after["enabled"] is False
    assert mcp_core.fingerprint_for_store(store) != fp_before


def test_delete_server(store, monkeypatch):
    _fake_envelope(monkeypatch)
    mcp_core.upsert_server(store, {"name": "jira", "url": "https://a.example.com", "headers": {}})
    body, status = mcp_core.delete_server(store, "jira")
    assert status == 200 and body == {"deleted": "jira"}
    body, _ = mcp_core.list_servers(store)
    assert body == {"servers": []}
    body, status = mcp_core.delete_server(store, "jira")
    assert status == 404
    assert body["error"]["kind"] == "not_found"


def test_fingerprint_changes_per_mutation(store, monkeypatch):
    _fake_envelope(monkeypatch)
    assert mcp_core.fingerprint_for_store(store) == ""
    mcp_core.upsert_server(store, {"name": "jira", "url": "https://a.example.com", "headers": {}})
    fp1 = mcp_core.fingerprint_for_store(store)
    assert fp1 != ""
    mcp_core.upsert_server(store, {"name": "confluence", "url": "https://b.example.com", "headers": {}})
    fp2 = mcp_core.fingerprint_for_store(store)
    assert fp2 != fp1
    mcp_core.delete_server(store, "confluence")
    fp3 = mcp_core.fingerprint_for_store(store)
    assert fp3 != fp2 and fp3 == fp1


def test_envelopes_payload_shape(store, monkeypatch):
    _fake_envelope(monkeypatch)
    mcp_core.upsert_server(store, {"name": "jira", "url": "https://a.example.com", "headers": {}})
    body, status = mcp_core.envelopes_payload(store)
    assert status == 200
    assert body["fingerprint"] == mcp_core.fingerprint_for_store(store)
    (srv,) = body["servers"]
    assert set(srv) == {"name", "enabled", "config_envelope"}


def _spy_wakes(store, monkeypatch):
    """Count the store-local + cross-worker wake calls _save fires."""
    calls = {"waiters": 0, "wake_bus": 0}
    monkeypatch.setattr(
        store, "notify_chat_waiters",
        lambda: calls.__setitem__("waiters", calls["waiters"] + 1))
    monkeypatch.setattr(
        mcp_core.wake_bus, "notify",
        lambda channel, uid: calls.__setitem__("wake_bus", calls["wake_bus"] + 1))
    return calls


def test_save_wakes_chat_poller_on_upsert(store, monkeypatch):
    _fake_envelope(monkeypatch)
    calls = _spy_wakes(store, monkeypatch)
    _, status = mcp_core.upsert_server(
        store, {"name": "jira", "url": "https://a.example.com", "headers": {}})
    assert status == 200
    assert calls == {"waiters": 1, "wake_bus": 1}


def test_save_wakes_chat_poller_on_set_enabled(store, monkeypatch):
    _fake_envelope(monkeypatch)
    mcp_core.upsert_server(store, {"name": "jira", "url": "https://a.example.com", "headers": {}})
    calls = _spy_wakes(store, monkeypatch)
    _, status = mcp_core.set_enabled(store, "jira", {"enabled": False})
    assert status == 200
    assert calls == {"waiters": 1, "wake_bus": 1}


def test_save_wakes_chat_poller_on_delete(store, monkeypatch):
    _fake_envelope(monkeypatch)
    mcp_core.upsert_server(store, {"name": "jira", "url": "https://a.example.com", "headers": {}})
    calls = _spy_wakes(store, monkeypatch)
    _, status = mcp_core.delete_server(store, "jira")
    assert status == 200
    assert calls == {"waiters": 1, "wake_bus": 1}
