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


def test_tick_defers_spawn_while_genesis_in_progress(monkeypatch):
    # "先 genesis 后 spawn": a host user whose import genesis is still running must
    # NOT boot a blank consumer; once genesis is done the next tick spawns.
    procs = FakeProcTable()
    sup = _sup(procs)
    monkeypatch.setattr(
        db, "get_blob",
        lambda uid, kind: {"status": "processing"} if kind == "genesis_state" else None)
    sup.tick(_roster("u_1"))
    assert procs.spawned == []                      # deferred while genesis runs
    monkeypatch.setattr(
        db, "get_blob",
        lambda uid, kind: {"status": "done"} if kind == "genesis_state" else None)
    sup.tick(_roster("u_1"))
    assert {s[1] for s in procs.spawned} == {"u_1"}  # genesis done → spawned


def test_tick_spawns_fresh_start_user_with_no_genesis(monkeypatch):
    # No genesis_state blob = fresh start (never uploaded) → must still spawn.
    procs = FakeProcTable()
    sup = _sup(procs)
    monkeypatch.setattr(db, "get_blob", lambda uid, kind: None)
    sup.tick(_roster("u_1"))
    assert {s[1] for s in procs.spawned} == {"u_1"}


def test_tick_enqueues_introduction_after_spawn(monkeypatch):
    procs = FakeProcTable()
    enqueued = []
    sup = Supervisor(
        owner="sup_A",
        lease_ttl=300.0,
        data_root="/agent-data",
        spawn_fn=procs.spawn,
        alive_fn=procs.is_alive,
        kill_fn=procs.kill,
        now=lambda: T0,
        introduction_enqueuer=lambda user_id, entry, **kwargs: enqueued.append((user_id, entry, kwargs)) or {"job_id": "pj_intro"},
    )
    monkeypatch.setattr(db, "get_blob", lambda uid, kind: None)

    sup.tick(_roster("u_1"))
    sup.tick(_roster("u_1"))

    assert len(enqueued) == 1
    assert enqueued[0][0] == "u_1"
    assert enqueued[0][1]["api_key"] == "key-u_1"
    assert enqueued[0][2]["api_url"]
    assert enqueued[0][2]["enclave_url"] is not None


def test_enqueue_introduction_job_when_profile_fields_empty(monkeypatch):
    class IntroStore:
        user_id = "u_1"

        def __init__(self):
            self.jobs = []

        def list_proactive_jobs(self, since_epoch=0, limit=0):
            return list(self.jobs)

        def append_proactive_job(self, job):
            self.jobs.append(job)
            return job

    store = IntroStore()
    monkeypatch.setattr(
        supervisor_mod,
        "_fetch_identity_plain_for_intro",
        lambda entry, **kwargs: ({"decrypt_status": "ok", "self_introduction": "", "signature": []}, ""),
    )

    job = supervisor_mod._enqueue_introduction_job_if_needed(
        "u_1",
        {"api_key": "k"},
        api_url="http://backend",
        enclave_url="https://enclave",
        now=lambda: T0,
        get_store_fn=lambda _uid: store,
    )
    duplicate = supervisor_mod._enqueue_introduction_job_if_needed(
        "u_1",
        {"api_key": "k"},
        api_url="http://backend",
        enclave_url="https://enclave",
        now=lambda: T0 + 1,
        get_store_fn=lambda _uid: store,
    )

    assert job["job_kind"] == "introduction"
    assert job["trigger"] == "post_spawn_genesis"
    assert job["source"] == "agent_initiated_proactive"
    assert job["status"] == "pending"
    assert duplicate is None
    assert len(store.jobs) == 1


def test_enqueue_introduction_skips_existing_profile(monkeypatch):
    monkeypatch.setattr(
        supervisor_mod,
        "_fetch_identity_plain_for_intro",
        lambda entry, **kwargs: ({"decrypt_status": "ok", "self_introduction": "I am here.", "signature": []}, ""),
    )

    job = supervisor_mod._enqueue_introduction_job_if_needed(
        "u_1",
        {"api_key": "k"},
        api_url="http://backend",
        enclave_url="https://enclave",
        get_store_fn=lambda _uid: (_ for _ in ()).throw(AssertionError("store should not be read")),
    )

    assert job is None


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


def test_tick_respawn_updates_lease_driver_column():
    # After an in-place driver switch (codex→claude on an API-key change), the
    # lease row's `driver` column must reflect the NEW driver — otherwise anything
    # reading the lease (ops, dashboards) sees a stale agent. Regression: in-place
    # respawn renewed the lease without updating driver, leaving it at the old value.
    procs = FakeProcTable()
    sup = _sup(procs)
    sup.tick([{"user_id": "u_1", "api_key": "k", "driver": "codex", "provider": "openai"}])
    assert leases.get("u_1")["driver"] == "codex"

    sup.tick([{"user_id": "u_1", "api_key": "k", "driver": "claude", "provider": "anthropic"}])
    assert leases.get("u_1")["driver"] == "claude"


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


# ---- Stage D: zero-roster credential resolution via runtime token ----


def test_auth_headers_prefers_runtime_token():
    assert supervisor_mod._auth_headers(runtime_token="tok") == {"X-Feedling-Runtime-Token": "tok"}
    assert supervisor_mod._auth_headers(api_key="k") == {"X-API-Key": "k"}


def test_resolve_discovered_builds_entries_via_token_without_api_key(monkeypatch):
    # host-all zero-roster: the supervisor knows only the user_id (from the DB).
    # It mints a runtime token and uses it to self-fetch + enclave-decrypt the
    # provider key — NO api_key anywhere.
    enabled = {"u1": {"driver": "claude", "provider": "anthropic", "model": "claude-x", "base_url": ""}}
    minted = {}

    def fake_mint(uid):
        minted[uid] = f"tok-{uid}"
        return minted[uid]

    monkeypatch.setattr(supervisor_mod, "_fetch_key_envelope",
                        lambda api_url, api_key="", runtime_token="": ({"ct": "x"} if runtime_token else None))
    monkeypatch.setattr(supervisor_mod, "_decrypt_provider_key",
                        lambda enclave_url, api_key="", envelope=None, runtime_token="": ("sk-ant" if runtime_token else ""))
    out = supervisor_mod._resolve_discovered(enabled, mint_token=fake_mint,
                                             api_url="http://b:5001", enclave_url="https://e:5003", cache={})
    e = {x["user_id"]: x for x in out}["u1"]
    assert e["provider_key"] == "sk-ant" and e["driver"] == "claude"
    assert "api_key" not in e                       # zero-roster: no api_key
    assert minted["u1"] == "tok-u1"


def test_resolve_discovered_caches_by_user_and_envelope(monkeypatch):
    # Re-resolving the same user with the same envelope must NOT re-hit the enclave
    # every tick (decrypt is a network call).
    enabled = {"u1": {"driver": "claude", "provider": "anthropic", "model": "m", "base_url": ""}}
    calls = {"n": 0}
    monkeypatch.setattr(supervisor_mod, "_fetch_key_envelope",
                        lambda api_url, api_key="", runtime_token="": {"ct": "same"})

    def dec(enclave_url, api_key="", envelope=None, runtime_token=""):
        calls["n"] += 1
        return "sk"

    monkeypatch.setattr(supervisor_mod, "_decrypt_provider_key", dec)
    cache = {}
    supervisor_mod._resolve_discovered(enabled, mint_token=lambda u: "t",
                                       api_url="a", enclave_url="e", cache=cache)
    supervisor_mod._resolve_discovered(enabled, mint_token=lambda u: "t",
                                       api_url="a", enclave_url="e", cache=cache)
    assert calls["n"] == 1                           # same envelope → decrypted once


def test_effective_roster_host_all_uses_discovered_entries():
    # When host-all supplies pre-credentialed discovered entries, they ARE the
    # roster (no api_key roster needed); base_roster only overrides by user_id.
    discovered = [{"user_id": "u1", "driver": "claude", "provider": "anthropic",
                   "model": "m", "base_url": "", "provider_key": "sk"}]
    roster, gateways = supervisor_mod._effective_roster(
        [], autodiscover=False, gateway_enabled=False, host_all_discovered=discovered)
    assert [e["user_id"] for e in roster] == ["u1"]
    assert roster[0]["provider_key"] == "sk" and "api_key" not in roster[0]


def test_effective_roster_host_all_base_roster_overrides_by_user_id():
    # A dev-supplied base_roster entry (with api_key) wins over discovery for the
    # same user — lets an operator pin a specific credential locally.
    discovered = [{"user_id": "u1", "driver": "claude", "provider": "anthropic", "provider_key": "sk-disc"}]
    base = [{"user_id": "u1", "api_key": "k", "driver": "claude", "provider": "anthropic", "provider_key": "sk-dev"}]
    roster, _ = supervisor_mod._effective_roster(
        base, autodiscover=False, gateway_enabled=False, host_all_discovered=discovered)
    assert len(roster) == 1 and roster[0]["provider_key"] == "sk-dev"


# ---- P1: _discover_enabled threads include_gateway to DB (host_all removed) ----


def test_discover_enabled_threads_include_gateway_to_db(monkeypatch):
    # host_all parameter removed; _discover_enabled now only passes include_gateway
    captured = {}

    def fake_list(include_gateway=False):
        captured["include_gateway"] = include_gateway
        return []

    monkeypatch.setattr(supervisor_mod.db, "list_agent_runtime_enabled_users", fake_list)
    supervisor_mod._discover_enabled(include_gateway=True)
    assert captured == {"include_gateway": True}


# ---- Codex P2: preserve a cached provider key across transient credential failures ----


def test_resolve_discovered_keeps_cached_key_on_transient_fetch_failure(monkeypatch):
    enabled = {"u1": {"driver": "claude", "provider": "anthropic", "model": "m", "base_url": ""}}
    state = {"env": {"ct": "x"}}
    monkeypatch.setattr(supervisor_mod, "_fetch_key_envelope",
                        lambda api_url, api_key="", runtime_token="": state["env"])
    monkeypatch.setattr(supervisor_mod, "_decrypt_provider_key",
                        lambda enclave_url, api_key="", envelope=None, runtime_token="": "sk-good")
    cache = {}
    out1 = supervisor_mod._resolve_discovered(enabled, mint_token=lambda u: "t",
                                              api_url="a", enclave_url="e", cache=cache)
    assert out1[0]["provider_key"] == "sk-good"
    # fetch fails this tick → must keep the cached key, NOT drop it (else a healthy
    # consumer respawns keyless on a transient blip).
    state["env"] = None
    out2 = supervisor_mod._resolve_discovered(enabled, mint_token=lambda u: "t",
                                              api_url="a", enclave_url="e", cache=cache)
    assert out2[0]["provider_key"] == "sk-good"


def test_resolve_discovered_keeps_cached_key_on_decrypt_failure(monkeypatch):
    enabled = {"u1": {"driver": "claude", "provider": "anthropic", "model": "m", "base_url": ""}}
    envs = iter([{"ct": "x"}, {"ct": "y"}])   # envelope changes → forces a re-decrypt
    monkeypatch.setattr(supervisor_mod, "_fetch_key_envelope",
                        lambda api_url, api_key="", runtime_token="": next(envs))
    decs = iter(["sk-good", ""])              # 2nd decrypt fails
    monkeypatch.setattr(supervisor_mod, "_decrypt_provider_key",
                        lambda enclave_url, api_key="", envelope=None, runtime_token="": next(decs))
    cache = {}
    supervisor_mod._resolve_discovered(enabled, mint_token=lambda u: "t",
                                       api_url="a", enclave_url="e", cache=cache)
    out2 = supervisor_mod._resolve_discovered(enabled, mint_token=lambda u: "t",
                                              api_url="a", enclave_url="e", cache=cache)
    assert out2[0]["provider_key"] == "sk-good"   # failed re-decrypt keeps last good key


# ---- auto verify_loop: open the bootstrap gate for a freshly-hosted user ----


def test_autoverify_triggers_once_then_marks_done():
    # A freshly-hosted user is dead-ended at needs_live_connection until verify_loop
    # runs once. Once passing, it is marked done and never re-triggers.
    calls = []
    state = {}

    def post_verify(api_url, headers):
        calls.append(headers)
        return True   # passing

    supervisor_mod._maybe_autoverify("u1", mint_token=lambda u: "t", api_url="a",
                                     state=state, post_verify=post_verify, now=lambda: 100.0)
    supervisor_mod._maybe_autoverify("u1", mint_token=lambda u: "t", api_url="a",
                                     state=state, post_verify=post_verify, now=lambda: 100.0)
    assert len(calls) == 1                          # second call skipped (done)
    assert calls[0] == {"X-Feedling-Runtime-Token": "t"}
    assert state["u1"]["done"] is True


def test_autoverify_backs_off_after_failure():
    # A user that can't pass yet (e.g. still at needs_identity) must NOT be re-probed
    # every tick — back off so we don't generate avoidable verify traffic.
    calls = []
    state = {}
    clock = {"t": 0.0}

    def post_verify(api_url, headers):
        calls.append(clock["t"])
        return False   # never passes

    now = lambda: clock["t"]
    supervisor_mod._maybe_autoverify("u1", mint_token=lambda u: "t", api_url="a",
                                     state=state, post_verify=post_verify, now=now)
    assert len(calls) == 1                          # first probe at t=0
    supervisor_mod._maybe_autoverify("u1", mint_token=lambda u: "t", api_url="a",
                                     state=state, post_verify=post_verify, now=now)
    assert len(calls) == 1                          # immediate retry suppressed (backoff)
    clock["t"] = 10_000.0
    supervisor_mod._maybe_autoverify("u1", mint_token=lambda u: "t", api_url="a",
                                     state=state, post_verify=post_verify, now=now)
    assert len(calls) == 2                          # probes again only after the window


def test_autoverify_stops_probing_once_it_passes_after_failures():
    state = {}
    clock = {"t": 0.0}
    results = iter([False, True])

    def post_verify(api_url, headers):
        return next(results)

    now = lambda: clock["t"]
    supervisor_mod._maybe_autoverify("u1", mint_token=lambda u: "t", api_url="a",
                                     state=state, post_verify=post_verify, now=now)
    assert state["u1"]["done"] is False             # failed → backing off
    clock["t"] = 10_000.0
    supervisor_mod._maybe_autoverify("u1", mint_token=lambda u: "t", api_url="a",
                                     state=state, post_verify=post_verify, now=now)
    assert state["u1"]["done"] is True              # passed → done
    called = {"n": 0}

    def pv2(api_url, headers):
        called["n"] += 1
        return True

    clock["t"] = 99_999.0
    supervisor_mod._maybe_autoverify("u1", mint_token=lambda u: "t", api_url="a",
                                     state=state, post_verify=pv2, now=now)
    assert called["n"] == 0                         # done → never probes again


def test_supervisor_heartbeat_payload_shape():
    """每 tick 写入 server_config 的全局心跳载荷：ts + owner + host_all + gateway。
    backend 的 wedge 守卫据此判断 supervisor 是否在托管。"""
    p = supervisor_mod._supervisor_heartbeat_payload(
        "host:7", host_all=True, gateway=False, ts=123.5)
    assert p == {"ts": 123.5, "owner": "host:7", "host_all": True, "gateway": False}
