# VPS resident distill — continuation / handoff (2026-07-07)

Spec: `docs/superpowers/specs/2026-07-07-vps-resident-distill-design.md` (v3, Codex-reviewed).
Branch: `feat/vps-resident-distill` (off `origin/test`).

## Done + verified (this session)

| commit | what | tests |
|---|---|---|
| `5662a12` | design spec v3 | — |
| `df61df2` | **P1** distill-mode gating + `sealed_v1` schema + bidirectional hard validation (`genesis_core.py`) | 9 unit + 17 plaintext-route regression |
| `5e4de78` | **P2 DB** resident atomic claim + migration `0013` (`awaiting_resident` status, claim cols) | 3 DB |
| `7ba3ccf` | **P2 DB** resident lease: heartbeat (owner-only) + stale reap (re-queue under cap / fail at cap) | 4 DB |

Resident **job-lifecycle DB layer is complete + verified**: `db.genesis_claim_resident_jobs`,
`db.genesis_resident_heartbeat`, `db.genesis_reap_stale_resident_jobs` (all `FOR UPDATE SKIP LOCKED`,
mirror the CVM worker functions). Worker `uploaded` claim and resident `awaiting_resident` claim never collide.

## Env runbook (how to run backend tests here)

```bash
# Postgres (conftest expects postgres:test@127.0.0.1:55432)
docker run -d --name feedling-test-pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16
# backend venv (already built this session at scratchpad/backend-venv):
python3 -m venv <venv> && <venv>/bin/pip install -r backend/requirements.txt pytest pytest-asyncio requests
# run (schema = alembic upgrade head via conftest.init_schema; users need seed_user() for the FK):
<venv>/bin/python -m pytest tests/test_genesis_resident_claim.py tests/test_genesis_resident_lease.py -q -p no:cacheprovider
```
Notes: Python 3.14 + psycopg_pool prints a benign `PythonFinalizationError` at interpreter exit in
*combined* runs — run files individually to confirm pass counts. FK: `genesis_import_jobs.user_id →
users` (migration 0012), so tests must `from conftest import seed_user; seed_user(uid)` before creating a job.

## Remaining (in order; env ready)

### P2-request-layer (backend)
- ✅ **DONE (`6dd038e`)** — Resident upload branch `_resident_sealed_import` (replaces the 501): size-check →
  store ciphertext via `genesis_put_chunk` (seq 0) → `genesis_create_job(status="awaiting_resident")` → return
  `{job:{status:"processing"}}`. Idempotent. Size limit `resident_distill_max_bytes()` /
  `FEEDLING_RESIDENT_DISTILL_MAX_BYTES` (default 512KiB, on decoded `body_ct`). 5 tests. **Upload↔claim loop
  wired.** ✅ **RECONCILED** — the sealed body now carries the **full v1 content-envelope** (`{format:"sealed_v1",
  client_job_id, mode, envelope:{v,id,body_ct,nonce,K_user,K_enclave,owner_user_id,visibility,enclave_pk_fpr}}`),
  the SAME `ContentEncryption.Envelope.jsonBody()` shape memory.add/identity/chunk already use → the enclave decrypts
  it unchanged. Backend stores `body_ct`→`encrypted_body`, the rest→`aad`; `/pending` rebuilds `{"envelope":{...}}`.
  Owner-mismatch (403) + incomplete-envelope (400) guarded. (Real enclave AEAD decrypt still = real-VPS e2e.)
- ✅ **DONE (`899b8e9`)** — Resident endpoints on `genesis/routes_asgi.py`, **per-user auth** (`require_auth` — the
  consumer authenticates as its own user, same as chat poll; claim scoped to that user, no host-all needed):
  - `GET /v1/genesis/resident/pending?consumer_id=` → `genesis_core.resident_pending` (claim + return sealed material).
  - `POST /v1/genesis/resident/{job_id}/heartbeat` → `genesis_core.resident_heartbeat` (owner-only).
  - `POST /v1/genesis/resident/{job_id}/complete` → `genesis_core.resident_complete` (done + `genesis_delete_chunks`).
  5 core tests + asgi-genesis (32) regression. **Full server-side loop verified: upload→pending→complete/heartbeat.**
- **TODO (minor)** — app-facing `job.status` map: `GET /v1/genesis/imports/{id}` (`get_import_status`) should map
  `awaiting_resident`→`processing` so the app never sees the internal status between upload and claim.
- **TODO** — wire a reaper tick calling `genesis_reap_stale_resident_jobs(1800, max_attempts=3, ...)` into the
  supervisor loop that already calls `genesis_reap_stale_processing_jobs`.

### P3 (backend) — `identity.replace` server-build action ✅ DONE (`<this commit>`)
- `_identity_replace_action` in `backend/identity/actions.py`, in the supported list + dispatch.
- Reuses `genesis.service.replace_identity_preserving_anchor` (server builds the shared envelope, agent sends
  plaintext, anchor preserved). Agent never builds an envelope.
- HIGH-RISK gating: payload must NOT carry `envelope`; requires `source=genesis_resident_distill` + `job_id` + `reason`,
  and `db.genesis_get_job(user, job_id)` must be a live resident job (status=processing, resident_consumer_id set).
- 5 tests. **⚠️ Implication for P6 skill (below): this gate means identity.replace is ONLY usable from the app-upload
  path (which has a resident job). The chat-handed-file path (entry B) has NO job → it must use
  `identity.profile_patch`/`identity.dimension_nudge` (incremental), NOT replace.**

## Remaining — NOT verifiable in this env (need real runtimes)

### P4 (feedling-mcp/tools/chat_resident_consumer.py) — resident consumer plumbing ✅ DONE (`<this commit>`)
`_process_resident_distill_once()` + `genesis_resident_pending/heartbeat/complete` + `_decrypt_sealed_material`,
wired into `run()` behind `FEEDLING_GENESIS_RESIDENT_ENABLED` (default off; 404 self-disables). Pure helpers
(`_parse_distill_output`, `_build_distill_prompt`) unit-verified here; the decrypt/agent/write loop needs the real
VPS e2e. The distill PROMPT is a MINIMAL default (belongs to the resident skill / Seven — refine there). Concrete
contract (backend is built + verified):
- `GET {API}/v1/genesis/resident/pending?consumer_id=<stable-id>` (user's api key/runtime token) →
  `{jobs:[{job_id, mode, sealed:{envelope:{v,id,body_ct,nonce,K_user,K_enclave,owner_user_id,visibility,enclave_pk_fpr}}}]}`.
- For each job: **POST `{"envelope": sealed["envelope"]}` to `FEEDLING_ENCLAVE_URL` `/v1/envelope/decrypt`** (the SAME
  decrypt the consumer already does for chat/memory — the envelope is the identical v1 shape) → write plaintext to a
  temp file → invoke the agent: "absorb `<path>` (mode=<mode>)".
  Agent distills per skill and returns memory cards + an optional identity payload. During a long distill call
  `POST /v1/genesis/resident/{job_id}/heartbeat {consumer_id}`.
- On finish: `POST /v1/genesis/resident/{job_id}/complete {memory_action_count, identity_status}` → backend marks done +
  deletes the stored ciphertext. Delete the temp file.
- **⚠️ CORRECTED crypto contract (verified against the backend, the earlier "send plaintext, bypass
  `_capture_build_envelope`" note was WRONG):** the write path is SPLIT:
  - **memory.add → the consumer builds the v1 envelope CLIENT-side** (reuse `_capture_build_envelope`,
    `chat_resident_consumer.py:5280`) and POSTs it via `execute_memory_actions` → `/v1/memory/actions`, which HARD-requires
    an envelope (`backend/memory/memory_core.py:287` "envelope required — v1 encryption is mandatory"). The agent returns
    cards (no keys); the consumer seals them, exactly like the capture lane. There is NO server-plaintext memory lane.
  - **identity.replace → the consumer sends PLAINTEXT** `{"type":"identity.replace","source":"genesis_resident_distill",
    "job_id":...,"reason":...,"identity":{...}}` via `execute_identity_actions` → `/v1/identity/actions`; the SERVER builds
    the envelope (`_identity_replace_action` REJECTS a client `envelope`, 400). This is the only server-build lane.
  - So: memory = client-seal (has keys, like capture); identity = server-build (P3 gate). Don't conflate the two.

### P5 (feedling-mcp-ios) — client seal + upload (needs Xcode/device)
- self-hosted branch of the 3 MaterialSheets (`GardenMaterialSheet`/`IdentityMaterialSheet`/`ChatEmptyStateView`):
  when `storageMode == .selfHosted`, instead of `uploadGenesisPlaintext` (plaintext), **seal the material** (reuse
  `sealForCurrentUser(plaintext:itemID:)` → `.jsonBody()`, `FeedlingAPI.swift:1309`) and POST a `sealed_v1` body:
  `{format:"sealed_v1", client_job_id, mode, envelope:{...the full ContentEncryption.Envelope.jsonBody envelope dict...}}`
  to `/v1/genesis/imports/plaintext`. **Upload-time size check** (~512KiB, self-hosted only). Cloud/worker path stays
  plaintext (unchanged). ✅ envelope shape is now the proven v1 wire format the backend + enclave already accept.

### P6 (io-onboarding) — resident skill
- entry-B (chat-handed file): memory md → light capture + `memory.add` (Dream reconciles); identity md → **incremental
  `identity.profile_patch`/`dimension_nudge`, NOT replace** (entry B has no genesis job, so identity.replace's gate
  rejects it — and incremental is the right semantics for a casual hand-off anyway).
- The app-upload path (entry A) does the full replace, but that's driven by the consumer (P4), not the chat skill.

### E2E — real VPS deploy (red line: verify AEAD/enclave real decrypt).

## Cleanup
- Postgres container `feedling-test-pg` may still be running: `docker rm -f feedling-test-pg`.
