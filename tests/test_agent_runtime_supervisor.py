"""DB-backed tests for the multi-user supervisor's tick orchestration.

Uses the real lease table with injected spawn/alive functions, so it exercises
acquire → spawn → heartbeat → reap and the cross-supervisor isolation that P1's
acceptance ("two users concurrent, leases/home not shared") rests on.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from agent_runtime import leases
from agent_runtime import supervisor as supervisor_mod
from agent_runtime.supervisor import Supervisor, parse_roster

T0 = 2_000_000.0


@pytest.fixture(autouse=True)
def _clean_table():
    with db.get_pool().connection() as conn:
        conn.execute("TRUNCATE agent_runtime_instances")
    yield


class FakeProcTable:
    """Hands out fake pids and tracks which are 'alive'."""

    def __init__(self):
        self.spawned = []   # (entry, user_id, home)
        self.alive = {}     # pid -> bool
        self.killed = []    # pids we were asked to terminate
        self._next = 1000

    def spawn(self, entry, user_id, home):
        self._next += 1
        pid = self._next
        self.alive[pid] = True
        self.spawned.append((entry, user_id, home))
        return pid

    def is_alive(self, pid):
        return self.alive.get(pid, False)

    def kill(self, pid):
        self.killed.append(pid)
        self.alive[pid] = False


def _roster(*uids):
    return [{"user_id": u, "api_key": f"key-{u}"} for u in uids]


def _sup(procs, owner="sup_A", clock=lambda: T0):
    return Supervisor(owner=owner, lease_ttl=300.0, data_root="/agent-data",
                      spawn_fn=procs.spawn, alive_fn=procs.is_alive,
                      kill_fn=procs.kill, now=clock)


def test_tick_spawns_one_consumer_per_user_with_isolated_homes():
    procs = FakeProcTable()
    sup = _sup(procs)
    sup.tick(_roster("u_1", "u_2"))

    assert {s[1] for s in procs.spawned} == {"u_1", "u_2"}
    homes = {s[1]: s[2] for s in procs.spawned}
    assert homes["u_1"] == "/agent-data/users/u_1"
    assert homes["u_2"] == "/agent-data/users/u_2"
    assert homes["u_1"] != homes["u_2"]
    # Both leases held by this supervisor.
    assert leases.get("u_1")["lease_owner"] == "sup_A"
    assert leases.get("u_2")["lease_owner"] == "sup_A"


def test_tick_is_idempotent_for_live_children():
    procs = FakeProcTable()
    sup = _sup(procs)
    sup.tick(_roster("u_1"))
    sup.tick(_roster("u_1"))   # child still alive → no respawn, just heartbeat
    assert len(procs.spawned) == 1


def test_other_supervisor_cannot_steal_a_live_lease():
    procs_a = FakeProcTable()
    sup_a = _sup(procs_a, owner="sup_A")
    sup_a.tick(_roster("u_1"))

    procs_b = FakeProcTable()
    sup_b = _sup(procs_b, owner="sup_B")
    sup_b.tick(_roster("u_1"))   # u_1's lease is live and owned by A

    assert procs_b.spawned == []
    assert leases.get("u_1")["lease_owner"] == "sup_A"


def test_dead_child_is_reaped_and_respawned():
    procs = FakeProcTable()
    sup = _sup(procs)
    sup.tick(_roster("u_1"))
    # The child dies.
    pid = sup.children["u_1"]["pid"]
    procs.alive[pid] = False

    sup.tick(_roster("u_1"))      # detects death → release + reacquire + respawn
    assert len(procs.spawned) == 2
    assert sup.children["u_1"]["pid"] != pid


def test_tick_reaps_child_no_longer_in_roster():
    # The live roster is re-derived each tick (autodiscover / gateway toggles), so a
    # user who gets disabled drops out. Their consumer must be killed + lease
    # released this tick, not left orphaned until lease expiry.
    procs = FakeProcTable()
    sup = _sup(procs)
    sup.tick(_roster("u_1", "u_2"))
    pid2 = sup.children["u_2"]["pid"]

    sup.tick(_roster("u_1"))            # u_2 dropped from the roster
    assert pid2 in procs.killed
    assert "u_2" not in sup.children
    assert leases.get("u_2")["lease_owner"] is None   # lease released for the dropped user
    assert "u_1" in sup.children        # u_1 untouched


def test_tick_respawns_alive_child_when_config_changes():
    # Per-tick re-derivation can change a live user's driver/provider/model (e.g.
    # autodiscover flips them, or native→gateway). The running consumer's env/home
    # is then stale → it must be restarted, not just heartbeated.
    procs = FakeProcTable()
    sup = _sup(procs)
    sup.tick([{"user_id": "u_1", "api_key": "k", "driver": "claude", "provider": "anthropic"}])
    pid1 = sup.children["u_1"]["pid"]

    sup.tick([{"user_id": "u_1", "api_key": "k", "driver": "codex", "provider": "openai"}])
    assert pid1 in procs.killed
    pid2 = sup.children["u_1"]["pid"]
    assert pid2 != pid1
    assert sup.children["u_1"]["entry"]["driver"] == "codex"   # registry holds new config
    assert len(procs.spawned) == 2
    assert leases.get("u_1")["lease_owner"] == "sup_A"         # lease retained across restart


def test_tick_no_respawn_when_config_unchanged():
    procs = FakeProcTable()
    sup = _sup(procs)
    e = {"user_id": "u_1", "api_key": "k", "driver": "claude", "provider": "anthropic"}
    sup.tick([dict(e)])
    sup.tick([dict(e)])      # identical config → heartbeat only
    assert procs.killed == []
    assert len(procs.spawned) == 1


def test_tick_gateway_upstream_key_rotation_does_not_respawn_consumer():
    # For a gateway user the upstream provider_key goes to LiteLLM, NOT the consumer
    # env — rotating it must not bounce the (heavy) consumer process.
    procs = FakeProcTable()
    sup = _sup(procs)
    e = {"user_id": "u_1", "api_key": "k", "driver": "codex", "provider": "gemini",
         "model": "gw-u_1", "provider_key": "k1"}
    sup.tick([dict(e)])
    sup.tick([{**e, "provider_key": "rotated"}])
    assert procs.killed == []
    assert len(procs.spawned) == 1


def test_heartbeat_advances_lease_expiry():
    procs = FakeProcTable()
    t = {"v": T0}
    sup = _sup(procs, clock=lambda: t["v"])
    sup.tick(_roster("u_1"))
    exp1 = leases.get("u_1")["lease_expires_at"]
    t["v"] = T0 + 100
    sup.tick(_roster("u_1"))      # heartbeat
    exp2 = leases.get("u_1")["lease_expires_at"]
    assert exp2 > exp1


def test_losing_lease_kills_the_orphaned_child():
    # sup_A spawns u_1, then misses heartbeats long enough that sup_B takes over
    # the expired lease. sup_A's next tick must kill its now-orphaned child so two
    # consumers don't both run (the "exactly one consumer per user" guarantee).
    t = {"v": T0}
    procs_a = FakeProcTable()
    sup_a = _sup(procs_a, owner="sup_A", clock=lambda: t["v"])
    sup_a.tick(_roster("u_1"))
    pid = sup_a.children["u_1"]["pid"]

    t["v"] = T0 + 400  # past sup_A's lease expiry (ttl 300)
    sup_b = _sup(FakeProcTable(), owner="sup_B", clock=lambda: T0 + 400)
    sup_b.tick(_roster("u_1"))                 # sup_B takes over
    assert leases.get("u_1")["lease_owner"] == "sup_B"

    sup_a.tick(_roster("u_1"))                 # sup_A re-ticks; child still "alive"
    assert pid in procs_a.killed               # orphan terminated
    assert "u_1" not in sup_a.children


def test_shutdown_releases_all_leases_and_kills_children():
    procs = FakeProcTable()
    sup = _sup(procs)
    sup.tick(_roster("u_1", "u_2"))
    pids = {sup.children["u_1"]["pid"], sup.children["u_2"]["pid"]}
    sup.shutdown()
    assert set(procs.killed) == pids
    assert leases.get("u_1")["lease_owner"] is None
    assert leases.get("u_2")["lease_owner"] is None


def test_parse_roster_accepts_json_string_and_list():
    parsed = parse_roster('[{"api_key": "k1"}, {"api_key": "k2", "model": "m"}]')
    assert [e["api_key"] for e in parsed] == ["k1", "k2"]
    assert parse_roster([{"api_key": "k3"}])[0]["api_key"] == "k3"


def test_parse_roster_drops_entries_without_api_key():
    assert parse_roster('[{"model": "m"}, {"api_key": "ok"}]') == [{"api_key": "ok"}]


# ---- Stage B: supervisor self-fetches the provider-key envelope (no roster secret) ----


def test_resolve_roster_self_fetches_envelope_when_entry_has_only_api_key(monkeypatch):
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enc")
    monkeypatch.setattr(supervisor_mod, "_whoami", lambda api_url, api_key: "usr_1")
    monkeypatch.setattr(
        supervisor_mod, "_fetch_key_envelope",
        lambda api_url, api_key: {"ct": "cipher"} if api_key == "k1" else None,
    )
    monkeypatch.setattr(
        supervisor_mod, "_decrypt_provider_key",
        lambda enclave_url, api_key, env: "sk-real" if env == {"ct": "cipher"} else "",
    )
    out = supervisor_mod._resolve_roster([{"api_key": "k1"}])
    assert out[0]["user_id"] == "usr_1"
    assert out[0]["provider_key"] == "sk-real"
    assert "provider_key_envelope" not in out[0]


def test_token_writer_invoked_on_spawn_and_renew():
    # Stage D slice 3a: the supervisor refreshes each user's runtime-token file
    # when it spawns the consumer AND on every heartbeat (so a short-lived token
    # is kept fresh for the long-running child).
    procs = FakeProcTable()
    writes = []
    sup = Supervisor(owner="sup_A", lease_ttl=300.0, data_root="/agent-data",
                     spawn_fn=procs.spawn, alive_fn=procs.is_alive, kill_fn=procs.kill,
                     now=lambda: T0, token_writer=lambda uid, home: writes.append((uid, home)))
    sup.tick(_roster("u1"))
    assert ("u1", "/agent-data/users/u1") in writes   # written at spawn
    writes.clear()
    sup.tick(_roster("u1"))                            # live child → heartbeat
    assert ("u1", "/agent-data/users/u1") in writes   # refreshed on renew


def test_no_token_writer_is_a_noop():
    procs = FakeProcTable()
    sup = Supervisor(owner="sup_A", lease_ttl=300.0, data_root="/agent-data",
                     spawn_fn=procs.spawn, alive_fn=procs.is_alive, kill_fn=procs.kill,
                     now=lambda: T0)  # no token_writer
    sup.tick(_roster("u1"))  # must not raise
    assert procs.spawned == [({"user_id": "u1", "api_key": "key-u1"}, "u1", "/agent-data/users/u1")]


# ---- codex gateway wiring (LiteLLM) ----


def test_gateway_entries_selects_only_codex_gateway_users():
    roster = [
        {"user_id": "a", "driver": "claude", "provider": "anthropic", "provider_key": "ka"},
        {"user_id": "b", "driver": "codex", "provider": "openai", "provider_key": "kb"},   # native
        {"user_id": "c", "driver": "codex", "provider": "openai_compatible", "model": "g",
         "base_url": "https://my.host/v1", "provider_key": "kc"},
    ]
    gw = supervisor_mod._gateway_entries(roster)
    assert [e["user_id"] for e in gw] == ["c"]
    assert gw[0]["provider"] == "openai_compatible"
    assert gw[0]["model"] == "g"
    assert gw[0]["base_url"] == "https://my.host/v1"  # custom endpoint → LiteLLM api_base
    assert gw[0]["provider_key"] == "kc"  # upstream key carried for LiteLLM env


def test_drop_gateway_users_filters_when_gateway_disabled():
    # With the gateway off, codex-gateway users must NOT be spawned (no proxy to
    # reach) — they're dropped so enabling hosted for them stays inert, not broken.
    roster = [
        {"user_id": "a", "driver": "claude", "provider": "anthropic"},
        {"user_id": "b", "driver": "codex", "provider": "openai"},          # native — kept
        {"user_id": "c", "driver": "codex", "provider": "gemini", "model": "g"},  # gateway — dropped
    ]
    kept = supervisor_mod._drop_gateway_users(roster)
    assert [e["user_id"] for e in kept] == ["a", "b"]


def test_effective_roster_autodiscover_off_gateway_off_drops_gateway_users():
    base = [
        {"user_id": "a", "driver": "claude", "provider": "anthropic", "api_key": "k"},
        {"user_id": "c", "driver": "codex", "provider": "gemini", "model": "g", "api_key": "k", "provider_key": "pk"},
    ]
    roster, gateways = supervisor_mod._effective_roster(base, autodiscover=False, gateway_enabled=False)
    assert [e["user_id"] for e in roster] == ["a"]   # gemini gateway user dropped
    assert gateways == []


def test_effective_roster_gateway_on_wires_models():
    base = [{"user_id": "c", "driver": "codex", "provider": "gemini",
             "model": "gemini-2.0-flash", "api_key": "k", "provider_key": "pk"}]
    roster, gateways = supervisor_mod._effective_roster(base, autodiscover=False, gateway_enabled=True)
    assert {e["user_id"]: e["model"] for e in roster} == {"c": "gw-c"}   # codex requests gw-id
    assert gateways and gateways[0]["model"] == "gemini-2.0-flash"        # real model → LiteLLM


def test_effective_roster_autodiscover_intersects_enabled(monkeypatch):
    base = [{"user_id": "a", "api_key": "k1"}, {"user_id": "b", "api_key": "k2"}]
    monkeypatch.setattr(supervisor_mod, "_discover_enabled",
                        lambda include_gateway: {"a": {"driver": "claude", "provider": "anthropic",
                                                       "model": "x", "base_url": ""}})
    roster, gateways = supervisor_mod._effective_roster(base, autodiscover=True, gateway_enabled=False)
    assert [e["user_id"] for e in roster] == ["a"]   # b not backend-enabled → excluded
    assert roster[0]["driver"] == "claude"


def test_effective_roster_empty_is_tolerated_not_fatal(monkeypatch):
    # A live agent-runner must idle (not exit/crashloop) when no user is enabled yet
    # — discovery returns nothing → empty effective roster, no exception.
    monkeypatch.setattr(supervisor_mod, "_discover_enabled", lambda include_gateway: {})
    roster, gateways = supervisor_mod._effective_roster([], autodiscover=True, gateway_enabled=False)
    assert roster == [] and gateways == []


def test_wire_gateway_models_swaps_requested_model_to_gw_id():
    roster = [
        {"user_id": "c", "driver": "codex", "provider": "gemini", "model": "gemini-2.0-flash", "provider_key": "kc"},
        {"user_id": "b", "driver": "codex", "provider": "openai", "model": "gpt-4o", "provider_key": "kb"},
    ]
    wired, gateways = supervisor_mod._wire_gateway_models(roster)
    by = {e["user_id"]: e for e in wired}
    # the gateway user's codex now REQUESTS the gw-<uid> model (LiteLLM maps it)
    assert by["c"]["model"] == "gw-c"
    # native openai user's model is untouched
    assert by["b"]["model"] == "gpt-4o"
    # but the LiteLLM routing keeps the user's REAL upstream model
    assert gateways[0]["user_id"] == "c"
    assert gateways[0]["model"] == "gemini-2.0-flash"


def test_wire_gateway_models_noop_without_gateway_users():
    roster = [{"user_id": "b", "driver": "codex", "provider": "openai", "model": "gpt-4o"}]
    wired, gateways = supervisor_mod._wire_gateway_models(roster)
    assert gateways == []
    assert wired == roster


def test_resolve_roster_prefers_existing_secret_over_self_fetch(monkeypatch):
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enc")
    monkeypatch.setattr(supervisor_mod, "_whoami", lambda api_url, api_key: "usr_2")
    called = {"fetched": False}

    def _no_fetch(api_url, api_key):
        called["fetched"] = True
        return None

    monkeypatch.setattr(supervisor_mod, "_fetch_key_envelope", _no_fetch)
    # A roster entry that already carries a plaintext provider_key must not trigger
    # a backend fetch (dev path / explicit override wins).
    out = supervisor_mod._resolve_roster([{"api_key": "k2", "provider_key": "sk-explicit"}])
    assert out[0]["provider_key"] == "sk-explicit"
    assert called["fetched"] is False
