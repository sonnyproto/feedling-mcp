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
  `FEEDLING_RESIDENT_DISTILL_MAX_BYTES` (default 512KiB, on stored ciphertext). 4 DB tests. **Upload↔claim loop
  wired.** ⚠️ the sealed-envelope field shape (`ciphertext_b64`/`ciphertext_sha256`/`aad`) is provisional — reconcile
  with the iOS sealer (P5) + verify on real enclave e2e before merge.
- **TODO — Resident-facing endpoints** (new router or add to `genesis/routes_asgi.py`), consumer-auth (runtime token):
  - `GET /v1/genesis/resident/pending` → `genesis_claim_resident_jobs(consumer_id=...)` (returns claimed jobs + a
    fetch handle for the sealed material).
  - `POST /v1/genesis/resident/{job_id}/heartbeat` → `genesis_resident_heartbeat`.
  - `POST /v1/genesis/resident/{job_id}/complete` → `genesis_complete_job(...)` + delete the stored material.
  - material fetch: reuse `genesis_list_chunks` (the consumer decrypts via `FEEDLING_ENCLAVE_URL`).
- **app-facing `job.status`**: only `processing/done/failed` (the resident `awaiting_resident/claimed` detail stays
  internal; `fetchGenesisJob` maps unknown→processing but Garden has an active-status whitelist — keep it to the 3).
- A background reaper tick calling `genesis_reap_stale_resident_jobs(1800, max_attempts=3, ...)` (wire into the
  existing supervisor/reaper loop that already calls `genesis_reap_stale_processing_jobs`).

### P3 (backend) — `identity.replace` server-build action
- New action in `backend/identity/actions.py` (`_identity_replace_action`), added to the supported-action list.
- Reuse/extract `service.replace_identity_preserving_anchor` (`genesis/service.py:786`) semantics: accept **plaintext**
  full identity + runtime token → server builds the shared envelope (`core_envelope._build_shared_envelope_for_store`,
  as `profile_patch` does) → replace, preserve anchor. **Agent never builds an envelope.**
- **HIGH-RISK gating (Codex P1)**: require `source="genesis_resident_distill"` + `job_id` + `reason`; backend validates
  the job belongs to the caller and is resident `claimed/running`. Payload **must not** carry an envelope (test this).

### P4 (feedling-mcp/tools) — resident consumer plumbing
- In `chat_resident_consumer.py`: poll `GET /resident/pending` → decrypt the sealed material (enclave) → write to a
  local temp file → hand the **path** to the agent ("absorb <path>") → agent distills per skill (memory.add /
  identity.replace) → `POST /complete` → delete temp file. Heartbeat during long distills.
- **Bypass** the existing `_capture_build_envelope` local-envelope lane (`chat_resident_consumer.py:5280`) — resident
  distill writes plaintext actions only (Codex P2).

### P5 (feedling-mcp-ios) — sub-agent candidate (unverifiable here)
- self-hosted branch of the three MaterialSheets: seal the material client-side (reuse `sealForCurrentUser` /
  `genesisPutChunk` envelope path, `FeedlingAPI.swift:1265/3047`) + `format:"sealed_v1"` body + upload-time size check.
  Worker/cloud body stays plaintext (`uploadGenesisPlaintext` unchanged).

### P6 (io-onboarding) — sub-agent candidate (docs)
- resident skill entry-B: identity md → local derive → **replace via `identity.replace`** (once P3 lands);
  memory md → light capture + Dream. (Skill already mostly aligned; finalize identity=replace wording.)

### E2E — real VPS deploy (red line: verify AEAD/enclave real decrypt).

## Cleanup
- Postgres container `feedling-test-pg` may still be running: `docker rm -f feedling-test-pg`.
