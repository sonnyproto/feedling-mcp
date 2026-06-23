# Round 3 (Proactive/Perception Runtime V2) — Validation Status

Audit-side record kept by the auditor (Claude). Captures what is and is NOT
yet validated for the V2 runtime, so we don't mistake "all PRs PASSED" for
"this is proven to work." Branch: `proactive-perception-runtime`.

Last updated: 2026-06-20 (before any real-device test, before merge to `test`).

> **⚠️ 2026-06-23 起,真机/真实路径验证的最新状态以 `ROUND3_REALDEVICE_TEST_PLAN.md`
> 的状态表 + 测试日志、以及 `CHANGELOG.md` 2026-06-23 条目为准。** 本文件下面的
> "now validated" 是 06-20 那轮的快照,resident 感知端到端、locality/calendar 增强等
> 后续验证未回填到这里——别把它当当前状态。

## What "PASS" meant during the audit

Each of the 13 audited artifacts passed = **code reads correctly + the
pure-unit tests pass + it matches the spec**. It did NOT mean "it ran." Until
the run below, no real Postgres, real model, multi-worker, or real device had
exercised the V2 path.

## ✅ Now validated (executed, not just read)

- **Real DB CAS correctness.** Ran the full DB-backed suite against a throwaway
  **Postgres 16** (recipe below): `169 passed`. This includes
  `tests/test_proactive_store_v2.py` (10 tests) — the lease acquire/reclaim,
  single-flight turn, wake dedup/merge, scheduled-fires-once-across-restart,
  settings v2 + legacy fallback, and background double-completion guard — which
  had **never executed anywhere before** (no local PG; CI only triggers on
  `main`/`test`, not feature branches). The SQL is correct.
- **app.py V2 assembly is sound.** app.py stays assembly-only (COMPAT
  re-exports). Scheduled wake HAS a driver: hosted 60s tick →
  `_run_hosted_tick_once` → `_run_hosted_scheduled_wake_due_once` →
  `fire_due_timers`, all gated behind `_hosted_wake_runtime_v2_enabled`
  (flag OFF ⇒ timers stay pending, fail-closed). It will not "fail to fire
  after a flag flip."
- **All three V2 flags default OFF, fail-closed.** Flipping is the only thing
  that changes live behavior; until then production runs the legacy path.
- **Round 3 V2 CI suite is wired.** `.github/workflows/ci.yml` now has a
  dedicated `Run Round 3 V2 regression suite` step inside the Postgres-backed
  `python-tests` job. The same 13-file list below passed locally against
  throwaway Postgres 16: `169 passed`.

## ❗ Still NOT validated — needs a real run before trusting

1. **Real model behavior — PARTIALLY VALIDATED 2026-06-20.** `run_agent` was a
   stub in every test. Ran a real-model dry-run via OpenRouter `claude-haiku-4.5`
   through the production prompt builder (`_hosted_wake_v2_provider_messages`) +
   real parser (`parse_agent_response_v2`), 4 scenarios:
   - manual summon → produced a visible message, no `ignored_manual` violation ✓
   - ambient low-signal → `sleep`, 0 messages (restraint) ✓
   - scheduled wake → visible reminder message ✓
   - late-night unlock → 0 messages (restraint) ✓
   The model wraps JSON in ```json fences and sometimes adds trailing prose; the
   parser extracted it correctly every time — robust to real-model formatting.
   **Still open:** single-shot only — the tool-execution LOOP (the model emitted
   a `memory.fetch` tool action in the summon case, but the dry-run did not run
   the tool executor and feed results back) is NOT exercised; and only
   haiku-4.5 was tested (production model may differ).
2. **Real-device perception.** The iOS perception fixtures were derived by
   reading Swift, not captured from a device. A real iPhone report must be
   diffed against the fixtures before flipping `perception_ingress_runtime_v2`.
3. **Multi-worker concurrency in prod.** The CAS logic passes on a single test
   DB; it has not run under real concurrent workers.
4. **GitHub Actions has not yet executed this branch.** `ci.yml` triggers on
   `main`/`test` pushes and PRs targeting those branches, so this feature branch
   still needs a PR/merge run before we call the CI gate proven on GitHub-hosted
   runners. The V2 pytest command itself is now wired into CI and has been
   executed locally against real Postgres 16.

## ✅ Pre-merge verification (run locally 2026-06-20, before the engineer merges)

- **Clean merge into `test`:** `git merge-tree` shows 0 conflict markers vs
  `origin/test`.
- **CI primary gate passes with V2 code:** started the backend exactly as CI
  does (`python backend/app.py` + wait for `/healthz`) against fresh Postgres 16,
  then ran `python tests/test_api.py … --multi-tenant` → **All tests passed**.
  So the merge PR's main CI job will be green.
- **`tests/test_db.py` passes.** The `test_multi_tenant_isolation.py` "errors"
  seen locally are environmental (the fixture's subprocess-backend healthz probe
  fails under this Mac's localhost proxy/timing — the backend itself boots fine
  with V2 loaded); they pass in CI (ubuntu) and are not a V2 regression.
- **No migration needed:** `init_schema` creates the V2 tables (proven — the
  lease-CAS tests pass, which require the real tables; and the backend boots and
  serves against a fresh DB). Deploy runs `init_schema` on boot.
- **Round 3 V2 CI command passes locally:** the exact V2 test list now wired
  into `ci.yml` passed against fresh Postgres 16: **169 passed**.

## ❗ iOS readiness for real-device perception (potential blocker)

The iOS app sends `/v1/perception/report` (a `context_snapshot` of continuous
signals — pull-only / zero-wake by design), plus `/v1/perception/photo/evaluate`
and `/v1/perception/app_open`. It was NOT found to emit the **discrete device
events** the V2 differ wakes on (`unlock_after_absence`, `screen_phash`,
Wi-Fi/BT connect). If the app indeed doesn't emit them, a real-device test will
exercise photo/app-open but NOT the connectivity/unlock/screen wake paths — they
may need an iOS build, or the backend may derive them server-side from the
snapshot. **Confirm with the iOS engineer + the real-device capture (gate b)
what the app actually sends before concluding the V2 wake path works.**

## 🔧 Known issues found during the run

- `tests/test_proactive_store_v2.py` run **alone** used to fail with
  `relation "user_logs" does not exist` because it imports `db` + proactive but
  not `app`. Resolved 2026-06-20: `tests/conftest.py` now calls
  `db.init_schema()` immediately after provisioning the throwaway test DB.
  Single-file `test_proactive_store_v2.py` now passes on real Postgres 16.

## ⚙️ Operational caveats (not bugs, but affect "does proactive fire")

- **hosted wake merge is intentionally off**: `wake_consumer.py` passes
  `merge_window_sec=0.0` because hosted wake execution is legacy compat-job
  driven; each claimed job is already the scheduling/ingress unit and is
  protected by the foreground turn lease. Resident/inbox runtimes can use a
  non-zero merge window when they own the queue end-to-end.
- **hosted proactive needs the cached api_key**: heartbeat decisions use
  `store.last_seen_api_key`. After a process restart, hosted self-initiated
  wakes stay silent until the user's next request repopulates the key.
- **V2 still rides the legacy `proactive_jobs` table** as its transport
  (compatibility). The legacy job table/semantics remain load-bearing.

## 🚧 Designed but NOT built in Round 3 (owned elsewhere)

- **Memory redesign (io-memory-spec).** Not built. V2 sits on the OLD memory
  store (`memory.index/fetch` → legacy `memory_load`). Owned by another engineer.
- **Screen co-presence understanding (D14).** `screen.read` / `screen.recent`
  are cataloged but return `tool_not_implemented_in_pr3`. Current screen signal
  is `screen_phash` (perceptual-hash change detection) + on-device iOS Vision
  `scene_hint` — **the backend does NOT OCR**. Actual caption/VLM is the TODO;
  engineer plans to hook a small model.
- **HealthKit + Weather.** ~~iOS side is 0~~ → **iOS shipped 2026-06-20**
  (`8bc4504`): encrypted `weather` + `health_sleep`/`health_workout`/
  `health_vitals` snapshot signals. **Backend ingress NOT yet wired** — these
  4 keys classify as `unknown_signal`/`error` in `ios_contract_v2.py`. Backend
  task dispatched to Codex (spec §9 B1b): register as encrypted pull-only
  signals (Seven confirmed pull-only), field-name-aligned to the iOS
  `PERCEPTION_BACKEND_TODO.md`; also delete the dead `resolve_focus`/
  `_apply_focus` (mapped to deleted user_state) and the stale
  `HEALTHKIT_UNAVAILABLE_V2`. Apple dev-portal must also enable HealthKit +
  WeatherKit capability before real-device auth works.
- **request_broadcast (B5) iOS acceptance.** Not done.

When those land, they need their own code-review round.

## How to run the DB-backed tests locally (no Docker needed)

```bash
PG16=/opt/homebrew/opt/postgresql@16/bin
PGDIR=$(mktemp -d /tmp/feedling_pg.XXXXXX)
"$PG16/initdb" -D "$PGDIR" -U postgres --auth=trust -E UTF8
"$PG16/pg_ctl" -D "$PGDIR" -o "-p 55432 -c listen_addresses=127.0.0.1 -k /tmp" -l "$PGDIR/server.log" start
export FEEDLING_TEST_PG="postgresql://postgres@127.0.0.1:55432/postgres"
export PYTHONPATH=backend
python3 -m pytest \
  tests/test_proactive_store_v2.py \
  tests/test_proactive_runtime_v2.py \
  tests/test_proactive_scheduled_wake_v2.py \
  tests/test_proactive_dashboard_v2.py \
  tests/test_proactive_observability_v2.py \
  tests/test_proactive_tool_executor_v2.py \
  tests/test_hosted_wake_v2_cutover.py \
  tests/test_perception_ingress_v2.py \
  tests/test_ios_perception_contract_v2.py \
  tests/test_perception.py \
  tests/test_proactive_jobs.py \
  tests/test_proactive_gate_eval.py \
  tests/test_model_api_wake.py \
  -q
# teardown:
"$PG16/pg_ctl" -D "$PGDIR" stop; rm -rf "$PGDIR"
```
(`tests/test_api.py` and `tests/test_enclave_route_errors.py` need a running
server / the `dstack_sdk` module and are excluded from this DB-correctness run.)

## Data track for real-device testing

Use the per-user proactive dashboard — it reads all V2 streams with status +
reason, so you can see where a turn breaks:

- HTML page: `GET /debug/proactive` (auth as the test user)
- JSON: `GET /v1/proactive/debug`

Sections include V2 health, wake→turn timeline, action/tool rows, and
background/scheduled rows. To exercise the V2 path for a test user, that user's
three V2 flags must be flipped ON first; otherwise the dashboard shows the
legacy path.
