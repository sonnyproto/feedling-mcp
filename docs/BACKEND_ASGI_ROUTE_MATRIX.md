# Backend ASGI migration — route accounting matrix

> Seeded 2026-07-03 from `docs/generated/backend-url-map-2026-07-03.txt`
> (133 Flask rules). This is the **hand-maintained 核销 ledger** for the
> FastAPI cutover; the `docs/generated/backend-url-map-*.txt` snapshot is the
> auto-generated baseline (regenerate + diff, never hand-edit).
>
> Plan: `docs/superpowers/specs/2026-07-03-backend-asgi-migration-plan.md`.

## Why this file is a cutover gate

The cutover has **no runtime Flask fallback** (plan §18). A route missed during
migration is not a soft failure — it is a user-visible **404**. So:

- **Every route below must reach `parity_status = pass` before prod cutover**
  (plan §13 切换条件, §18 红线 1). No exceptions, no "probably fine".
- A route is only checked off when it has a native `routes_asgi.py`
  implementation **and** an ASGI parity test — path existence alone is not
  accounting (plan §19.4).
- Before cutover, regenerate the snapshot and `diff` it against
  `backend-url-map-2026-07-03.txt`; any Flask-side route churn during the
  migration window (§6.6 双写) must be reflected here first (plan §6.1 保鲜).

Per-route parity must cover (plan §19.4): method/path/query/body parsing;
status code + error envelope; auth-failure / permission-failure; response
headers / CORS / compression / cache-control; legacy-client lax payloads;
cancellation / timeout; side effects (DB write, wake notify, background enqueue,
external provider call).

## Risk tiers & migration order (plan §6.2, §18 红线 2)

Migrate **A → B → C → D**. Never write the highest-risk path first.

| Tier | Meaning | Examples |
|---|---|---|
| **A** | Low-risk read routes | `healthz`, `whoami`, `bootstrap/status`, config/get reads |
| **B** | Normal write routes | identity/memory actions, push tokens, worldbook |
| **C** | High-risk user main path | `model_api/chat/send`, chat send/history, hosted turn, genesis import |
| **D** | Special runtime | poll waiters, admin observability, proactive tick/worker hooks, WS |

The waiting-I/O routes in **D** (`chat/poll`, `proactive/jobs/poll`) are the
migration bullseye and the whole point of the effort (plan §9); they land in
PR 6 on the async waiter registry, ahead of the bulk C-tier rewrite.

## Column legend

- **Owner** — Flask blueprint prefix = owning domain package = migration unit
  (plan §3.1). `hosted_*` blueprints all belong to the `hosted/` package;
  `chat_verify` to `chat/`.
- **Flask test** — `y(N)` = N test files under `tests/` reference this path's
  static prefix (a coverage *signal*, not a guarantee the assertion is strong);
  `—` = no reference found, needs a test authored before/with its migration.
- **ASGI test** — parity test against the FastAPI app (`httpx.ASGITransport`).
  `—` until authored.
- **Parity** — `pending` → `pass`. Only `pass` counts toward 核销.

## Out-of-band surface (not in the HTTP url_map)

| Surface | Owner | Notes |
|---|---|---|
| `:9998` screen WS ingest | screen | Daemon-thread asyncio loop + advisory-lock leader election (`core/leader.py`, `screen/ws.py`). **Not a Flask route** — decided separately in plan §12 (default: keep as-is thread). Tracked here so it isn't forgotten at cutover. |

---

## Tier A — low-risk reads (46)

| Path | Methods | Owner | Risk | Flask test | ASGI test | Parity | Notes |
|---|---|---|---|---|---|---|---|
| `/static/<path:filename>` | GET | (app) | A | — | — | **n/a (dropped)** | DEAD ROUTE — Flask auto-default; no static/ dir, no static_folder, zero /static/ consumers. ASGI intentionally omits it (both 404). Not counted toward the 132 to migrate. |
| `/v1/access/modes` | GET | accounts | A | y(2) | y (test_asgi_accounts_remaining) | **pass** | PR7 |
| `/v1/users/whoami` | GET | accounts | A | y(14) | y (test_asgi_whoami_bootstrap) | **pass** | PR 5 — native accounts/routes_asgi + whoami_core; auth dep + run_db + contextvar |
| `/v1/agent/perception` | GET | agent | A | y(6) | y (test_asgi_agent) | **pass** | PR 7 — perception_core |
| `/v1/agent/perception/digest` | GET | agent | A | y(4) | y (test_asgi_agent) | **pass** | PR 7 |
| `/v1/agent/perception/history` | GET | agent | A | — | y (test_asgi_agent) | **pass** | PR 7 |
| `/v1/agent/perception/trend` | GET | agent | A | — | y (test_asgi_agent) | **pass** | PR 7 |
| `/v1/bootstrap/status` | GET | bootstrap | A | y(4) | y (test_asgi_whoami_bootstrap) | **pass** | PR 5 — native bootstrap/routes_asgi + status_core |
| `/healthz` | GET | content | A | y(5) | y (test_asgi_healthz) | **pass** | PR 4 — native asgi/health.py, parity vs Flask oracle |
| `/v1/content/export` | GET | content | A | — | y (test_asgi_content) | **pass** | PR7 — raw Response + Content-Disposition; heavy read via run_db |
| `/v1/copytext` | GET | copytext | A | y(2) | y (test_asgi_copytext) | **pass** | PR 7 — public, ETag/304 preserved |
| `/v1/debug/trace` | GET | diagnostics | A | y(2) | y (test_asgi_diagnostics) | **pass** | PR7 |
| `/v1/onboarding/validate` | GET | hosted_onboarding_validation | A | y(4) | y (test_asgi_hosted_import_validation) | **pass** | PR7 |
| `/v1/memory/capture_jobs` | GET | hosted_setup_routes | A | — | y (test_asgi_hosted_setup) | **pass** | PR7 |
| `/v1/model_api/get` | GET | hosted_setup_routes | A | y(4) | y (test_asgi_hosted_setup) | **pass** | PR7 |
| `/v1/model_api/key_envelope` | GET | hosted_setup_routes | A | y(2) | y (test_asgi_hosted_setup) | **pass** | PR7 — returns own ciphertext, no decrypt |
| `/v1/model_api/runtime` | GET | hosted_setup_routes | A | — | y (test_asgi_hosted_setup) | **pass** | PR7 |
| `/v1/state/receipts` | GET | hosted_setup_routes | A | — | y (test_asgi_hosted_setup) | **pass** | PR7 |
| `/v1/identity/changes` | GET | identity | A | — | y (test_asgi_identity) | **pass** | PR7 |
| `/v1/identity/get` | GET | identity | A | y(15) | y (test_asgi_identity) | **pass** | PR7 |
| `/v1/identity/verify` | GET | identity | A | y(2) | y (test_asgi_identity) | **pass** | PR7 |
| `/v1/memory/buckets` | GET | memory | A | y(4) | y (test_asgi_memory) | **pass** | PR7 |
| `/v1/memory/get` | GET | memory | A | y(1) | y (test_asgi_memory) | **pass** | PR7 |
| `/v1/memory/list` | GET | memory | A | y(9) | y (test_asgi_memory) | **pass** | PR7 |
| `/v1/memory/threads` | GET | memory | A | y(2) | y (test_asgi_memory) | **pass** | PR7 |
| `/v1/memory/verify` | GET | memory | A | y(4) | y (test_asgi_memory) | **pass** | PR7 |
| `/v1/perception/app_open` | GET | perception | A | y(2) | y (test_asgi_perception) | **pass** | PR7 |
| `/v1/perception/items/<kind>` | GET | perception | A | — | y (test_asgi_perception) | **pass** | PR7 |
| `/v1/perception/photo/<photo_id>/content` | GET | perception | A | — | y (test_asgi_perception) | **pass** | PR7 — JSON metadata + enclave decrypt_path pointer (no server decrypt) |
| `/v1/perception/photos` | GET | perception | A | — | y (test_asgi_perception) | **pass** | PR7 |
| `/v1/perception/snapshot` | GET | perception | A | — | y (test_asgi_perception) | **pass** | PR7 |
| `/v1/proactive/decisions` | GET | proactive | A | y(2) | y (test_asgi_proactive_remaining) | **pass** | PR7 |
| `/v1/proactive/reviews` | GET | proactive | A | y(2) | y (test_asgi_proactive_remaining) | **pass** | PR7 |
| `/v1/push/tokens` | GET | push | A | y(1) | y (test_asgi_push) | **pass** | PR7 |
| `/v1/screen/analyze` | GET | screen | A | y(1) | y (test_asgi_screen) | **pass** | PR7 |
| `/v1/screen/frames` | GET | screen | A | y(7) | y (test_asgi_screen) | **pass** | PR7 |
| `/v1/screen/frames/<filename>` | GET | screen | A | y(7) | y (test_asgi_screen) | **pass** | PR7 |
| `/v1/screen/frames/<frame_id>/decrypt` | GET | screen | A | y(7) | y (test_asgi_screen) | **pass** | PR7 — enclave decrypt PROXY (no server plaintext) |
| `/v1/screen/frames/<frame_id>/envelope` | GET | screen | A | y(7) | y (test_asgi_screen) | **pass** | PR7 — opaque ciphertext, no decrypt |
| `/v1/screen/frames/<frame_id>/image` | GET | screen | A | y(7) | y (test_asgi_screen) | **pass** | PR7 — enclave image PROXY, bytes+Range preserved |
| `/v1/screen/frames/latest` | GET | screen | A | — | y (test_asgi_screen) | **pass** | PR7 |
| `/v1/screen/ios` | GET | screen | A | — | y (test_asgi_screen) | **pass** | PR7 |
| `/v1/screen/mac` | GET | screen | A | — | y (test_asgi_screen) | **pass** | PR7 |
| `/v1/screen/summary` | GET | screen | A | — | y (test_asgi_screen) | **pass** | PR7 |
| `/v1/sources` | GET | screen | A | — | y (test_asgi_screen) | **pass** | PR7 |
| `/v1/worldbook/list` | GET | worldbook | A | y(2) | y (test_asgi_worldbook) | **pass** | PR7 |

## Tier B — normal writes (43)

| Path | Methods | Owner | Risk | Flask test | ASGI test | Parity | Notes |
|---|---|---|---|---|---|---|---|
| `/v1/access/claim-token` | POST | accounts | B | y(2) | y (test_asgi_accounts_remaining) | **pass** | PR7 — PUBLIC (one-time token bearer) |
| `/v1/access/link-token` | POST | accounts | B | y(2) | y (test_asgi_accounts_remaining) | **pass** | PR7 |
| `/v1/access/modes/switch` | POST | accounts | B | y(2) | y (test_asgi_accounts_remaining) | **pass** | PR7 |
| `/v1/account/recover/challenge` | POST | accounts | B | y(2) | y (test_asgi_accounts_remaining) | **pass** | PR7 — PRE-AUTH challenge/response |
| `/v1/account/recover/verify` | POST | accounts | B | y(2) | y (test_asgi_accounts_remaining) | **pass** | PR7 — PRE-AUTH proof-of-possession |
| `/v1/onboarding/route` | GET,POST | accounts | B | y(4) | y (test_asgi_accounts_remaining) | **pass** | PR7 |
| `/v1/users/preferences` | POST | accounts | B | y(4) | y (test_asgi_accounts_remaining) | **pass** | PR7 |
| `/v1/bootstrap` | POST | bootstrap | B | y(5) | y (test_asgi_bootstrap_post) | **pass** | PR7 — POST; body ignored (matches Flask) |
| `/v1/content/rewrap-to-current-key` | POST | content | B | y(2) | y (test_asgi_content) | **pass** | PR7 — enclave rewrap (decrypt+rebuild), api_key forward |
| `/v1/content/swap` | POST | content | B | y(2) | y (test_asgi_content) | **pass** | PR7 — envelope relocate in place, no enclave |
| `/v1/users/public-key` | POST | content | B | y(2) | y (test_asgi_content) | **pass** | PR7 |
| `/v1/copytext` | POST | copytext | B | y(2) | y (test_asgi_copytext) | **pass** | PR 7 — admin-token gated (not user auth) |
| `/v1/debug/trace` | DELETE | diagnostics | B | y(2) | y (test_asgi_diagnostics) | **pass** | PR7 |
| `/v1/debug/trace/enable` | POST | diagnostics | B | y(2) | y (test_asgi_diagnostics) | **pass** | PR7 |
| `/v1/diagnostics/logs` | POST | diagnostics | B | y(2) | y (test_asgi_diagnostics) | **pass** | PR7 — R2 write via run_db |
| `/v1/identity/actions` | POST | identity | B | y(6) | y (test_asgi_identity) | **pass** | PR7 — require_scope('identity'), enclave forward |
| `/v1/identity/init` | POST | identity | B | y(7) | y (test_asgi_identity) | **pass** | PR7 — envelope build (server-side, same fn) |
| `/v1/identity/relationship_anchor` | POST | identity | B | — | y (test_asgi_identity) | **pass** | PR7 |
| `/v1/identity/replace` | POST | identity | B | y(2) | y (test_asgi_identity) | **pass** | PR7 |
| `/v1/memory/actions` | POST | memory | B | y(4) | y (test_asgi_memory) | **pass** | PR7 — require_scope('memory') |
| `/v1/memory/add` | POST | memory | B | y(5) | y (test_asgi_memory) | **pass** | PR7 |
| `/v1/memory/delete` | DELETE | memory | B | y(1) | y (test_asgi_memory) | **pass** | PR7 |
| `/v1/memory/fetch` | POST | memory | B | y(4) | y (test_asgi_memory) | **pass** | PR7 |
| `/v1/memory/index` | POST | memory | B | y(4) | y (test_asgi_memory) | **pass** | PR7 |
| `/v1/memory/legacy_batch` | POST | memory | B | y(4) | y (test_asgi_memory) | **pass** | PR7 — require_scope('memory'), enclave decrypt |
| `/v1/memory/migration_state` | GET,POST | memory | B | — | y (test_asgi_memory) | **pass** | PR7 — GET auth / POST scope('memory') |
| `/v1/memory/retype` | POST | memory | B | y(2) | y (test_asgi_memory) | **pass** | PR7 |
| `/v1/onboarding/archive` | POST | onboarding_archive | B | y(2) | y (test_asgi_onboarding_archive) | **pass** | PR7 — streamed R2 upload; 413 body JSON vs Flask HTML (status parity) |
| `/v1/perception/photo/evaluate` | POST | perception | B | — | y (test_asgi_perception) | **pass** | PR7 |
| `/v1/perception/report` | POST | perception | B | y(2) | y (test_asgi_perception) | **pass** | PR7 — enclave decrypt on ingress-v2 path, api_key forward |
| `/v1/device/events` | GET,POST | proactive | B | y(6) | y (test_asgi_proactive_remaining) | **pass** | PR7 |
| `/v1/proactive/decisions/<decision_id>/review` | POST | proactive | B | y(2) | y (test_asgi_proactive_remaining) | **pass** | PR7 |
| `/v1/proactive/settings` | GET,POST | proactive | B | y(2) | y (test_asgi_proactive_remaining) | **pass** | PR7 |
| `/v1/proactive/state` | GET,POST | proactive | B | y(2) | y (test_asgi_proactive_remaining) | **pass** | PR7 |
| `/v1/push/dynamic-island` | POST | push | B | — | y (test_asgi_push) | **pass** | PR7 |
| `/v1/push/live-activity` | POST | push | B | y(2) | y (test_asgi_push) | **pass** | PR7 |
| `/v1/push/live-start` | POST | push | B | — | y (test_asgi_push) | **pass** | PR7 |
| `/v1/push/notification` | POST | push | B | — | y (test_asgi_push) | **pass** | PR7 — APNs |
| `/v1/push/register-token` | POST | push | B | y(2) | y (test_asgi_push) | **pass** | PR7 |
| `/v1/track/event` | POST | tracking | B | y(2) | y (test_asgi_tracking) | **pass** | PR 7 |
| `/v1/worldbook/delete` | DELETE | worldbook | B | y(2) | y (test_asgi_worldbook) | **pass** | PR7 |
| `/v1/worldbook/match` | POST | worldbook | B | y(2) | y (test_asgi_worldbook) | **pass** | PR7 — enclave forward, E2E preserved |
| `/v1/worldbook/upsert` | POST | worldbook | B | y(2) | y (test_asgi_worldbook) | **pass** | PR7 — enclave forward, E2E preserved |

## Tier C — high-risk user main path (23)

| Path | Methods | Owner | Risk | Flask test | ASGI test | Parity | Notes |
|---|---|---|---|---|---|---|---|
| `/v1/users/register` | POST | accounts | C | y(54) | y (test_asgi_accounts_remaining) | **pass** | PR7 — PUBLIC (no auth); orphan-lineage backstop preserved (409) |
| `/v1/chat/history` | GET | chat | C | y(23) | y (test_asgi_chat_remaining) | **pass** | PR7 |
| `/v1/chat/history` | DELETE | chat | C | y(23) | y (test_asgi_chat_remaining) | **pass** | PR7 |
| `/v1/chat/message` | POST | chat | C | y(9) | y (test_asgi_chat_remaining) | **pass** | PR7 — envelope write + notify_chat_waiters (async wake) |
| `/v1/chat/messages/<message_id>/body` | GET | chat | C | y(2) | y (test_asgi_chat_remaining) | **pass** | PR7 |
| `/v1/chat/response` | POST | chat | C | y(11) | y (test_asgi_chat_remaining) | **pass** | PR7 — agent reply; reply-claim CAS; no explicit notify (matches Flask) |
| `/v1/account/reset` | POST | content | C | y(4) | y (test_asgi_content) | **pass** | PR7 — DESTRUCTIVE cascade, parity-verified (both backends purge same tables) |
| `/v1/genesis/imports` | GET | genesis | C | y(4) | y (test_asgi_genesis) | **pass** | PR7 — background enqueue (not inline, §5.7) |
| `/v1/genesis/imports` | POST | genesis | C | y(4) | y (test_asgi_genesis) | **pass** | PR7 — background enqueue (not inline, §5.7) |
| `/v1/genesis/imports/<job_id>` | GET | genesis | C | y(4) | y (test_asgi_genesis) | **pass** | PR7 |
| `/v1/genesis/imports/<job_id>/chunks/<int:seq>` | PUT | genesis | C | y(4) | y (test_asgi_genesis) | **pass** | PR7 |
| `/v1/genesis/imports/<job_id>/finalize` | POST | genesis | C | y(4) | y (test_asgi_genesis) | **pass** | PR7 |
| `/v1/genesis/imports/<job_id>/outputs` | POST | genesis | C | y(4) | y (test_asgi_genesis) | **pass** | PR7 — require_scope('genesis'), enclave forward |
| `/v1/genesis/imports/plaintext` | POST | genesis | C | y(2) | y (test_asgi_genesis) | **pass** | PR7 |
| `/v1/genesis/persona_backfill` | POST | genesis | C | — | y (test_asgi_genesis) | **pass** | PR7 — require_scope('genesis') |
| `/v1/model_api/chat/send` | POST | hosted_chat_routes | C | y(13) | y (test_asgi_hosted_chat_send) | **pass** | PR7 — CROWN JEWEL; 202 contract, single agent-runtime path, wait via run_db, all debug_trace preserved, no decrypt |
| `/v1/history_import/status/<job_id>` | GET | hosted_history_import | C | y(2) | y (test_asgi_hosted_import_validation) | **pass** | PR7 |
| `/v1/history_import/upload` | POST | hosted_history_import | C | y(2) | y (test_asgi_hosted_import_validation) | **pass** | PR7 — JSON, enqueues background job (not inline) |
| `/v1/model_api/delete` | DELETE | hosted_setup_routes | C | — | y (test_asgi_hosted_setup) | **pass** | PR7 |
| `/v1/model_api/driver` | POST | hosted_setup_routes | C | y(4) | y (test_asgi_hosted_setup) | **pass** | PR7 |
| `/v1/model_api/memory/repair` | POST | hosted_setup_routes | C | y(2) | y (test_asgi_hosted_setup) | **pass** | PR7 |
| `/v1/model_api/setup` | POST | hosted_setup_routes | C | y(9) | y (test_asgi_hosted_setup) | **pass** | PR7 — seal/reuse via same enclave fns |
| `/v1/model_api/test` | POST | hosted_setup_routes | C | — | y (test_asgi_hosted_setup) | **pass** | PR7 |

## Tier D — special runtime (21)

| Path | Methods | Owner | Risk | Flask test | ASGI test | Parity | Notes |
|---|---|---|---|---|---|---|---|
| `/v1/chat/poll` | GET | chat | D | y(9) | y (test_asgi_poll_native) | **pass** | **PR 6 bullseye done** — native async waiter (chat/routes_asgi), same-worker wake via store hook (§9.1/§19.2) |
| `/v1/proactive/jobs/poll` | GET | proactive | D | y(2) | y (test_asgi_poll_native) | **pass** | **PR 6 bullseye done** — native async waiter (proactive/routes_asgi); stale reclaim + clamp preserved (§9.2) |
| `/v1/proactive/jobs/<job_id>/claim` | POST | proactive | D | y(2) | y (test_asgi_proactive_remaining) | **pass** | PR7 |
| `/v1/proactive/jobs/<job_id>/status` | POST | proactive | D | y(2) | y (test_asgi_proactive_remaining) | **pass** | PR7 |
| `/v1/chat/verify_loop` | POST | chat_verify | D | y(3) | y (test_asgi_chat_remaining) | **pass** | PR7 — synthetic ping + notify |
| `/v1/capture/force` | POST | proactive | D | — | y (test_asgi_proactive_remaining) | **pass** | PR7 |
| `/v1/capture/tick` | POST | proactive | D | y(4) | y (test_asgi_proactive_remaining) | **pass** | PR7 |
| `/v1/dream/tick` | POST | proactive | D | y(2) | y (test_asgi_proactive_remaining) | **pass** | PR7 |
| `/v1/proactive/tick` | POST | proactive | D | y(4) | y (test_asgi_proactive_remaining) | **pass** | PR7 |
| `/v1/proactive/scheduled/actions` | POST | proactive | D | y(2) | y (test_asgi_proactive_remaining) | **pass** | PR7 |
| `/v1/proactive/scheduled/fire` | POST | proactive | D | y(4) | y (test_asgi_proactive_remaining) | **pass** | PR7 |
| `/v1/proactive/debug` | GET | proactive | D | y(2) | y (test_asgi_proactive_remaining) | **pass** | PR7 |
| `/debug/proactive` | GET | proactive | D | y(4) | y (test_asgi_proactive_remaining) | **pass** | PR7 — HTML; reuses Flask request-ctx (refactor before Flask删除) |
| `/admin/data-track` | GET | admin | D | y(2) | y (test_asgi_admin) | **pass** | PR7 — admin-token; HTML route reuses Flask request-ctx (refactor before Flask删除) |
| `/admin/data-track/users/<user_id>` | GET | admin | D | y(2) | y (test_asgi_admin) | **pass** | PR7 — admin-token; HTML route reuses Flask request-ctx (refactor before Flask删除) |
| `/v1/admin/data-track/dau` | GET | admin | D | y(2) | y (test_asgi_admin) | **pass** | PR7 |
| `/v1/admin/data-track/summary` | GET | admin | D | y(2) | y (test_asgi_admin) | **pass** | PR7 |
| `/v1/admin/data-track/users` | GET | admin | D | y(2) | y (test_asgi_admin) | **pass** | PR7 |
| `/v1/admin/data-track/users/<user_id>` | GET | admin | D | y(2) | y (test_asgi_admin) | **pass** | PR7 |
| `/v1/admin/store/evict` | POST | admin | D | y(2) | y (test_asgi_admin) | **pass** | PR7 |
| `/v1/admin/diagnostics/logs/<user_id>` | GET | diagnostics | D | y(2) | y (test_asgi_diagnostics) | **pass** | PR7 — admin-token gated |

---

## Totals

| Tier | Count | Parity pass |
|---|---:|---:|
| A | 46 | 45 (…+ hosted model_api reads ×5, onboarding/validate) |
| B | 43 | 43 (…+ bootstrap POST, onboarding/archive) |
| C | 23 | 23 (…+ hosted setup/test/driver/delete/repair, chat/send, history_import ×2) — COMPLETE
| D | 21 | 21 (…+ chat/verify_loop) |
| **Total** | **133** | **132 migrated + 1 static dropped = 133 / 133 accounted (100%)** |

The two D-tier polls are the migration's payoff routes (§9): idle polls now park
an asyncio future instead of a gunicorn thread. The remaining routes are
mechanical per-package migrations (PR 7…N) on the patterns established here
(`routes_asgi.py` + `*_core.py` payload + parity test).

Cutover is blocked until **Parity pass = 133 / 133** and the snapshot diff is
clean (plan §13, §18 红线 1).
