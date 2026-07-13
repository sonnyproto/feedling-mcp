# Agent-driven API-key qualification

This directory contains the first release-qualification slice for Feedling's
deployed Runtime V2. It intentionally covers API-key users only. VPS/OAuth,
iOS UI automation, and customer-incident replay remain separate follow-up
workstreams.

## What runs

The workflow is deliberately split across explicit trust zones:

1. `verify_deployment.py` uses the test admin credential before Codex starts and
   again after the agent finishes to require one unchanged backend build,
   homogeneous live-worker builds, and trusted pre/post candidate-SHA receipts
   outside the public artifact directory.
2. `provision_profiles.py` is a deterministic credential boundary. It creates
   eight fresh synthetic accounts, proves invalid-key rejection and valid-key
   recovery without accepting echoed credentials, enables user-scoped trace
   access, requires a server-side synthetic-account TTL/reaper before the first
   registration, and uses the test admin token to set and read back
   `db_action_v2`. A present-but-expired provider key becomes a fixed-code
   blocked row while provisioning continues through the other profiles, so a
   failed credential still produces a complete eight-row diagnostic matrix.
3. The provisioner output is deterministically split into eight owner-only
   one-row manifests. `run_codex_profile_workers.py` launches exactly eight
   independent top-level `codex exec` processes in three fixed batches (3+3+2),
   with at most three running concurrently. Each selected Codex profile exposes
   only its matching row and isolated home/temp/work roots; no process receives
   provider or admin credentials.
4. Every profile agent returns one structured `profileResult`. Trusted launcher
   code validates it against a profile-locked Structured Outputs schema, binds
   its hash and root Codex thread ID into an owner-only lifecycle receipt, keeps
   raw events/stderr quarantined, and copies only the validated JSON into a
   separate aggregation-input directory.
5. A separate headless Codex qualification supervisor reads only those eight
   validated profile results and the trusted receipt. It preserves each profile
   judgment, computes the run summary and orchestration projection, and returns
   the canonical JSON final message against
   `schemas/codex-run-result.schema.json`. Its parent writes to a fresh private
   path, and `publish_agent_result.py` installs `run-result.json` exclusively
   without following or replacing an agent-created link.
6. `render_artifacts.py` validates that canonical result against the richer,
   authoritative gate schema at `schemas/run-result.schema.json` and
   mechanically derives the coverage matrix, numeric latency CSV, body-free
   JUnit XML, and exact per-profile JSON documents.
7. `validate_run.py` is a deterministic fail-closed gate. It checks the schema,
   exact profile/scenario order, scenario-specific assertions/evidence/IDs,
   preserved retry observations, per-turn five-stage trace and numeric latency
   evidence, and nearest-rank p50/p95 summaries recomputed from those turns,
   one supervisor plus exactly eight uniquely assigned independent profile
   workers with no more than three observed concurrently, exact agreement with
   the trusted process/thread/hash receipt, Runtime V2 identity, unchanged trusted
   pre/post deployment receipts, exact binding to the owner-only read-only
   provisioning manifest, PASS statuses, and required artifact paths.
8. The workflow always resets every synthetic account and uploads only the
   public artifact directory after cleanup succeeds and an exact secret scan.

`codex_output_schema.py --check` proves offline that the checked-in Codex
authoring schema is the exact compatible projection of the gate schema plus
the locked per-scenario assertion maps. The authoring schema intentionally
drops constraints unsupported by Structured Outputs; it does not replace or
weaken deterministic release validation.

The locked matrix is:

- official DeepSeek
- official Anthropic/Claude
- official OpenAI/ChatGPT
- official Google Gemini
- OpenRouter Claude
- OpenRouter OpenAI/ChatGPT
- OpenRouter GLM
- Kongbeiqie OpenAI-compatible relay

Every profile runs `P0-01` through `P0-13`, including fresh onboarding, key
validation, four-part persona import/distillation, basic and ten-turn chat,
memory/persona consistency, model identity, reasoning disclosure, latency
attribution, trace correlation, and cleanup.

Persona qualification is deliberately two-phase. The existing-session capture
imports once and writes decrypted live evidence only to an isolated worker's
owner-mode `0600` temp file. That profile agent reads it and writes a bounded
semantic judgment tied to the capture SHA-256; deterministic finalization checks
the hash and judgment contracts, emits only sanitized evidence, and deletes the
plaintext on every exit path.

All eight profiles lock reasoning effort to `medium`. A provider default, omitted
setting, or disabled reasoning cannot produce a release PASS.

P0-12 also guards the failure chain recorded in
[Router entry mrj6pdgl-6dppch](https://router.feedling.app/entry?id=mrj6pdgl-6dppch):
a route merely reporting `medium` is insufficient. The exact
correlated turn must prove reasoning capability enabled, requested/configured/
effective effort all equal to `medium`, a positive provider-visible
reasoning/thinking event count, token metadata, and a nonempty user-visible
summary/disclosure. `reasoning:false`, an effective `off` clamp, or zero events
fails even if setup echoed the requested effort. The suite never requests or
stores a model's hidden private chain-of-thought.

## `QA_TEST_ADMIN_TOKEN`

`QA_TEST_ADMIN_TOKEN` is the client-side name for the credential accepted by
the **test** backend's admin routes. Its value must match that deployment's
`FEEDLING_ADMIN_TOKEN`. This is not issued by Feedling: the operator chooses one
strong random value, for example with `openssl rand -hex 32`, and stores it only
in secret managers. Deterministic QA tools use it only to call:

- `POST /v1/admin/hosted-runtime-mode`, to select `db_action_v2` for a newly
  created synthetic user; and
- `GET /v1/admin/hosted-runtime-mode`, to independently read the selection
  back;
- `GET /v1/admin/v2-metrics`, before and after Codex runs, to produce the
  trusted deployment receipts from `backend_sha`, homogeneous `worker_shas`,
  and live worker count; and
- `GET /v1/admin/data-track/users/{id}`, only after an ambiguous cleanup `401`,
  to prove that the synthetic account is absent before treating it as already
  reset; and
- `GET /v1/admin/qa/synthetic-account-reaper`, before creating any account, to
  require an enabled `agent-e2e-` label reaper with a maximum TTL no greater
  than four hours.

This token has broader test-admin authority because the backend shares one
admin credential across admin routes. Keep it in the protected
`feedling-e2e-test` GitHub Environment, never use the production token, and
never expose it to Codex, prompts, logs, or uploaded artifacts.

The same random **test-only** value has three names at three boundaries:

- `TEST_FEEDLING_ADMIN_TOKEN`: repository or organization Actions secret used
  by the `test` deployment job;
- `FEEDLING_ADMIN_TOKEN`: environment variable injected into the deployed test
  backend; and
- `QA_TEST_ADMIN_TOKEN`: protected `feedling-e2e-test` Environment secret used
  by deterministic qualification steps.

The production deployment continues to use the separate Actions secret named
`FEEDLING_ADMIN_TOKEN`. Its value MUST differ from the test value. Configure
`TEST_FEEDLING_ADMIN_TOKEN` before merging the CI change or the next test deploy
will intentionally fail closed.

## Test backend and runner infrastructure

“Test backend” means the existing non-production deployment behind
`https://test-api.feedling.app`, including its backend, database, Runtime V2
workers, and queues. If that environment is already isolated from production,
you do **not** need another Feedling VPS just for this suite. The system under
test remains the existing test deployment.

The headless test driver is separate infrastructure. This design requires one
single-job ephemeral GitHub Actions runner VM with the `feedling-e2e` label. It
holds the dedicated QA Codex OAuth bundle only for that job and is destroyed
afterward; it does not host Feedling or replace a Runtime V2 worker. The test app
deploy, test runner deploy, test Postgres deploy, and qualification workflow
share the `feedling-test-environment` concurrency lock. Pre/post build receipts
still catch a deployment made outside those workflows.

## One-time GitHub setup

Create a protected GitHub Environment named `feedling-e2e-test`. Configure its
deployment branch policy to allow only the protected `test` branch, and require
reviewers before the job can access the environment. The workflow also has a
`refs/heads/test` dispatch guard, which explicitly fails a workflow selected from
any other ref; that in-repository guard is defense in depth and does not replace
the Environment restriction. Add these environment **secrets**:

- `QA_CODEX_AUTH_JSON_B64`
- `QA_TEST_ADMIN_TOKEN`
- `QA_DEEPSEEK_API_KEY`
- `QA_ANTHROPIC_API_KEY`
- `QA_OPENAI_PROVIDER_API_KEY`
- `QA_OPENROUTER_API_KEY`
- `QA_GEMINI_API_KEY`
- `QA_KONGBEIQIE_API_KEY`

Add these non-secret environment **variables** with explicit, reasoning-capable
model IDs that the deployed candidate supports. Each selection must return the
reasoning metadata and token accounting required by `P0-12`; a model without
that capability correctly fails the release gate rather than silently reducing
coverage:

- `QA_CODEX_MODEL` (pin to `gpt-5.4` for the qualified Codex CLI contract)
- `QA_DEEPSEEK_MODEL`
- `QA_ANTHROPIC_MODEL`
- `QA_OPENAI_MODEL`
- `QA_GEMINI_MODEL`
- `QA_OPENROUTER_CLAUDE_MODEL`
- `QA_OPENROUTER_OPENAI_MODEL`
- `QA_OPENROUTER_GLM_MODEL`
- `QA_KONGBEIQIE_MODEL`
- `QA_KONGBEIQIE_BASE_URL` (the normalized HTTPS OpenAI-compatible endpoint)

`QA_CODEX_AUTH_JSON_B64` is the base64 encoding of a complete `auth.json` from a
**dedicated QA ChatGPT account**. This deliberately includes refreshable OAuth
credentials so a four-hour qualification job can use the account's subscription
without a manual login on every ephemeral runner. It is a high-value, long-lived
secret: never use a founder/engineer account, never paste it into a workflow
input, require Environment approval, and revoke/rotate the QA account session on
a schedule or immediately after suspected exposure. `codex login
--with-access-token` is not a substitute for this bundle in the pinned CLI: that
flag accepts Codex PAT/agent-identity credentials, not an ordinary ChatGPT OAuth
access token.

The workflow validates the bundle as refreshable ChatGPT auth, rejects API-key,
PAT, Bedrock, and agent-identity modes, installs it as mode `0600` under a
run-scoped `CODEX_HOME`, and masks each decoded token. The base64 bundle, decoded
JSON, ID token, access token, and refresh token are all included in the post-run
artifact secret scan.

Register a single-job ephemeral self-hosted runner with the labels `self-hosted`,
`linux`, `x64`, and `feedling-e2e`. It must pin `codex-cli 0.144.3`, support the
Codex Linux bubblewrap sandbox, run as a non-root account, and MUST NOT have
`$HOME/.codex/auth.json` or another persistent ChatGPT login. Destroy the runner
VM after every job; deleting the work directory alone is not sufficient.

The runner VM is ephemeral, and the workflow creates a fresh owner-only
`CODEX_HOME` for every run. Pinned Codex 0.144.3 does not reliably apply a
permission profile to native custom subagents, so this suite does not use that
mechanism. Every profile is instead a separate top-level invocation selected
with `-p <profile>`; its top-level `default_permissions` binding is checked by
strict config and real sandbox probes. Raw sessions, events, OAuth material, and
stderr remain private and disappear with the single-job runner.

The dedicated QA OAuth bundle is inside the trusted Codex-process boundary. Its
path is excluded from model-controlled shell environments and prompts, but the
suite does not pretend that Codex's own home can be sandboxed away from the
Codex process that must refresh it. Provider and admin keys remain wholly
outside that boundary. Each profile process receives a fresh empty
`HOME`/`TMPDIR`/work root and a deny-by-default permission profile: read-only
checkout access, read-only access to exactly one one-row synthetic-account
manifest, writes only to that worker's disposable roots, denial of public
artifacts, sibling manifests, raw worker outputs, aggregation inputs, the full
cleanup manifest, and the lifecycle receipt, disabled web/browser/apps/plugins
and login shells, and managed-proxy traffic only to `test-api.feedling.app`.
The aggregation supervisor has no manifest or raw-output access and runs with
network proxying disabled. Before provisioning, the workflow verifies OAuth,
strict profile selection, no configured MCP server, filesystem boundaries,
allowed test-API egress, and denied external/raw-IP bypasses. After provisioning,
it probes all eight exact mode-`0600` rows for own-read/other-deny isolation.

Keep an independent runner/VPC egress policy as a second boundary: the Codex
parent needs OpenAI/ChatGPT service access, while model-driven subprocesses should
reach only `test-api.feedling.app`. Prompt rules and artifact scanning are not
credential-isolation controls.

## Before a live run

The deployed `https://test-api.feedling.app` candidate must provide:

- the Runtime V2 admin set/readback routes;
- the admin-gated V2 metrics contract with `backend_sha`, `worker_shas`, and a
  positive live-worker count matching the candidate SHA;
- `db_action_v2` workers and queues;
- deploy-enabled, user-scoped traces; and
- the admin-gated synthetic-account reaper status contract, backed by a real
  server-side TTL/janitor for `agent-e2e-` labels; and
- observable backend and worker build identity that can be matched to the
  candidate commit.

Trigger **API-key Runtime V2 qualification** manually from the protected `test`
branch in GitHub Actions and enter the full deployed candidate commit SHA. Any
other selected ref explicitly fails before the protected Environment or its
secrets are reached. Manual mode is intentional for the first stabilization
phase; there is no push, schedule, or deployment trigger yet.

Do not launch a live run until the deployment contract below exists. The current
Runtime V2 implementation does not yet expose the raw backend/worker build SHA
fields or all test-control-plane routes required by the preflight/provisioner,
so the workflow will intentionally fail before semantic testing rather than test
an unknown or mixed deployment. Those are product/deployment prerequisites, not
gaps that this testing-only branch should fake or bypass.

The workflow's `always()` cleanup covers ordinary step failures, not runner loss,
job cancellation, or infrastructure termination. The single-job runner MUST be
ephemeral so its copied OAuth bundle is destroyed after the job, and the backend
still needs a server-side TTL/reaper for `agent-e2e-*` synthetic accounts so an
abruptly lost runner cannot strand test users indefinitely.

## Artifacts and release rule

`QA_ARTIFACT_DIR` is already the unique run directory. Codex returns only the
authoritative result JSON; the trusted publisher installs `run-result.json`, and
`render_artifacts.py` then derives `matrix.md`,
`latency.csv` (including numeric acknowledgement, reply, per-turn five-stage,
and profile-summary rows), `junit.xml`, and exact `profiles/<profile-id>.json`
copies directly beneath the same directory. No second run-ID directory is
created. Public files must never contain provider keys, Feedling account keys,
private content keys, raw chat, raw traces, raw private reasoning, or free-form
evidence/failure text.

The seven summary fields count the exact terminal statuses of the eight profiles
and must sum to eight. The gate is green only when all eight profiles and all
thirteen scenarios per profile are present in order and PASS with their locked
assertions, evidence codes, required IDs, and preserved attempt history; Runtime
V2 and unchanged pre/post candidate identity are proven; all chat turns have the
five required trace stages and numeric per-turn stage timing; cleanup succeeds;
required files exist; and the redaction scan is clean. A blocked prerequisite is
useful evidence, but it is never a release PASS.
