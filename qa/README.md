# Agent-driven API-key qualification

This directory contains the first deployed-runtime qualification slice for
Feedling API-key users. It intentionally covers API-key users only. VPS/OAuth,
iOS UI automation, and customer-incident replay remain separate follow-up
workstreams.

## What runs

There are two targets:

- **baseline** (the local driver's default) tests the runtime currently deployed
  on `test-api.feedling.app`, records its reported mode/version, and does not
  claim that a legacy `runtime_version: 2` label proves the new Hosted Runtime V2 architecture;
- **strict Hosted Runtime V2** is an opt-in target of the protected GitHub
  workflow and additionally requires admin mode selection, homogeneous
  worker/backend build identity, and V2 receipts.

The protected workflow is deliberately split across explicit trust zones:

1. `verify_deployment.py` checks endpoint liveness before Codex starts and again
   after the agent finishes. In strict V2 mode it also uses the test admin
   credential to require one unchanged backend build, homogeneous live-worker
   builds, and trusted pre/post candidate-SHA receipts outside the public
   artifact directory.
2. `provision_profiles.py` is a deterministic credential boundary. It creates
   eight fresh synthetic accounts, proves invalid-key rejection and valid-key
   recovery without accepting echoed credentials, enables user-scoped trace
   access, requires a server-side synthetic-account TTL/reaper before the first
   registration, and reads the configured runtime through the user API. In
   strict V2 mode it also uses the test admin token to set and independently
   verify `hosted_resident`. A present-but-expired provider key becomes a fixed-code
   blocked row while provisioning continues through the other profiles, so a
   failed credential still produces a complete eight-row diagnostic matrix.
3. The provisioner output is deterministically split into eight owner-only
   one-row manifests. `run_codex_profile_workers.py` launches exactly eight
   independent top-level `codex exec` processes in three fixed batches (3+3+2),
   with at most three running concurrently. Each selected Codex profile exposes
   only its matching row and isolated home/temp/work roots; no process receives
   provider or admin credentials.
4. Every profile agent returns one structured `profileResult`. Trusted launcher
   code requires a completed command beginning with the exact
   `QA_SCENARIO_ID=P0-XX` marker for every agent-driven scenario P0-02 through
   P0-11 (with separate P0-06 capture, evidence-review, and finalization tool
   calls), validates the result against a profile-locked Structured Outputs
   schema, validates and binds the private P0-12 receipt to the result's exact
   request/turn/trace IDs and bounded reasoning fields, and binds the result
   hash, event hash, COT receipt hash, and root Codex thread ID into an
   owner-only lifecycle receipt. Raw command text and events/stderr stay
   quarantined; only validated JSON enters the separate aggregation-input
   directory. A structurally valid COT product failure is preserved in the
   receipt and artifacts for the deterministic final gate to reject rather than
   being erased by an early launcher exception.
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
   the trusted process/thread/hash receipt, unchanged trusted pre/post liveness
   receipts, strict Runtime V2 identity when selected, exact binding to the owner-only read-only
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

## Run the currently deployed test build locally

`run_local_diagnostic.py` is the operator path for testing the existing
`https://test-api.feedling.app` deployment without changing the `test` branch,
deploying another Feedling backend, or provisioning a special VPS. The headless
Codex workers run on the operator's machine and use the existing ChatGPT OAuth
session in `~/.codex/auth.json`. Provider keys remain confined to the
deterministic provisioner and are never placed in a Codex prompt or worker
environment.

The default is baseline qualification: it accepts any configured runtime status,
records the observed mode/version, and runs the full user-behavior journey. Add
`--require-runtime-v2` only after the new Hosted Runtime V2 candidate is deployed.

Before copying that OAuth bundle, the local driver treats PATH only as a package
locator, derives the native binary from the pinned official npm layout, and
verifies the exact platform package file set, ownership/modes, version, and
whole-tree digest. It invokes the verified native binary rather than the PATH
wrapper and rejects an installation beneath the checkout, run-private roots,
OAuth directory, public artifacts, or system temporary directory. The first
local operator slice pins Codex `0.144.3` on macOS arm64; `--codex-bin` can name
that installation's npm wrapper or native binary explicitly, but cannot bypass
the provenance check.

The dotenv file must be an owner-only regular file:

```sh
chmod 600 /absolute/path/.env.test
```

A repository-local `.env.test` is supported, but the live checkout is never a
Codex read root. Before configuring the workers, the deterministic parent makes
an owner-only source snapshot containing only `qa/`, `tools/provider_smoke/`,
`tools/genesis_e2e.py`, and `backend/content_encryption.py`. Within that
allowlist it excludes `.env*`, dependency caches, prior qualification artifacts,
and the exact dotenv and OAuth source paths. It also rejects any copied source
file containing any provider or admin credential loaded from the dotenv,
including credentials for profiles omitted from a subset run. Workers receive
read access only to that sanitized snapshot.

First prove that the pinned Codex CLI, copied OAuth session, model selection,
isolated config, and one real headless `codex exec` invocation work. This step
does not create Feedling users or call provider endpoints:

```sh
python3 qa/run_local_diagnostic.py \
  --env-file /absolute/path/.env.test \
  --candidate-sha <full-deployed-source-sha> \
  --codex-model gpt-5.4 \
  --profile official-gemini \
  --preflight-only
```

Then remove `--preflight-only` to create one fresh synthetic account and run the
live Gemini canary. Repeat `--profile` to select a bounded subset, or omit it to
run the locked eight-profile matrix. The candidate SHA is the source commit
actually deployed by the test manifest, not automatically the tip of the Git
branch.

For the future strict runtime candidate, append `--require-runtime-v2` to the
same command.

Local output is written under `qualification-artifacts/<run-id>/`. The sanitized
source snapshot, manifests, and copied OAuth material stay under a run-scoped
owner-only directory. After verified account cleanup, a passing run removes that
directory. A non-passing worker run first copies a bounded,
credential-scanned subset of raw
worker events, stderr, scratch files, and Codex session evidence to the owner-only
`~/.codex/feedling-e2e-debug/<run-id>/` quarantine, explicitly excluding the
provisioning manifests and any file containing known provider, synthetic-user,
content, or OAuth credentials; it then removes the original private run. The
summary records only `private_debug_retained` and its run ID. If account cleanup
fails, the private run directory is reduced to exactly the owner-only original
provisioning manifest required for cleanup retry. The source snapshot, copied
OAuth, worker outputs, raw events, profile manifests, and every other private
file are deleted, and `private_cleanup_retry_retained` is true.
If private finalization itself fails, the run fails closed and attempts to
remove the entire original private root instead of retaining partially scrubbed
manifests or raw evidence. If rendering or the public secret scan fails, every
would-be public artifact is quarantined by deleting the artifact directory and
rebuilding it with only a fixed, sanitized `SECURITY_FAIL` summary.

The public diagnostic summary and matrix always say
`release_qualified: false`: this path proves deployed end-user behavior and
captures partial evidence, but it cannot substitute for protected deployment
SHA, server-side reaper, and full-matrix release attestations.
`DIAGNOSTIC_PASS` additionally requires every selected profile's trusted COT
receipt to prove the correct final answer, one correlated reasoning event,
reasoning metadata, and a delivered user-visible disclosure. A profile agent
cannot override a missing, failed, or mismatched receipt with a PASS judgment.
When an otherwise valid receipt disagrees with the agent-authored projection,
the matrix reports the gate failure (`COT_RESULT_BINDING_MISMATCH`) separately
from the receipt's trusted observation status/code, so the underlying product
failure is not hidden by an agent reporting mistake.
The summary also records the exact harness Git HEAD, dirty state, whole-harness
source digest, worker-source digest, and exact copied worker-snapshot digest;
the run aborts before Codex if the snapshot bytes differ from the measured
source bytes.

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

Codex is intentionally the semantic-judgment trust boundary, not an adversarial
program being cryptographically proved to have "thought." Deterministic code
proves ordered successful evidence access, rejects a fixed-path persona judgment
that already exists at REVIEW, binds the reviewed capture hash through the
Genesis finalizer, and validates the resulting schema/evidence. It cannot prove
the model's internal reasoning or defeat a deliberately deceptive judge that
manufactures an alternate prefill and copies it later; that would require a
second independent judge or a different trust model.

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
At P0-12 the worker writes a fixed request marker, and the trusted launcher runs
`cot_delivery_probe.py` once. The authoritative private receipt lives in that
profile's directory beneath the worker-output root, which the profile's
permission denies; the worker receives only a sanitized facts copy in its work
root. The receipt binds
the exact model-call trace, parsed-agent trace, stored reply ID, and decryptable
thinking envelope; the launcher validates and hashes that receipt before the
profile can be accepted as agent-authored diagnostic evidence. Missing provider
reasoning-token accounting remains explicitly unverified instead of being
invented from ordinary input/output token counts.
The launcher resolves one owner-controlled, crypto-capable Python executable,
fixes it as `QA_PYTHON_BIN` in every worker profile, grants only its narrow
runtime roots, and proves `"$QA_PYTHON_BIN" -I -B` can load the probe inside the
real sandbox before any synthetic account is provisioned. Workers may not build
their own virtual environments or install dependencies during qualification.

## `QA_TEST_ADMIN_TOKEN`

`QA_TEST_ADMIN_TOKEN` is the client-side name for the credential accepted by
the **test** backend's admin routes. Its value must match that deployment's
`FEEDLING_ADMIN_TOKEN`. This is not issued by Feedling: the operator chooses one
strong random value, for example with `openssl rand -hex 32`, and stores it only
in secret managers. The baseline local diagnostic does not use it. Protected
release qualification uses it for the test-account reaper and cleanup; strict
V2 mode additionally uses it to call:

- `POST /v1/admin/hosted-runtime-mode`, to select `hosted_resident` for a newly
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
`https://test-api.feedling.app`, including its backend, database, and whichever
runtime workers and queues are currently deployed. If that environment is already isolated from production,
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
`actions/setup-python` must install Python 3.12 into a narrow tool-cache runtime
owned by that same runner account: `sys.prefix`, `sys.base_prefix`, the runtime
`bin` directory, and the resolved executable must be owner-controlled and not
group/world writable, and the executable must resolve directly beneath a
runtime `bin`. A root-owned system Python or broad `/usr` prefix is unsupported.
The workflow validates this boundary before decoding the QA OAuth bundle or
provisioning synthetic accounts, so a misconfigured runner fails safely.

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

For a baseline local run, the deployed endpoint needs the existing API-key
onboarding, chat, persona, trace, and authenticated runtime-status contracts.
This path does not wait for the new Hosted Runtime V2 feature branch.

For the strict Hosted Runtime V2 GitHub release run, the deployed candidate must additionally provide:

- the Runtime V2 admin set/readback routes;
- the admin-gated V2 metrics contract with `backend_sha`, `worker_shas`, and a
  positive live-worker count matching the candidate SHA;
- `hosted_resident` Runtime V2 workers and queues;
- deploy-enabled, user-scoped traces; and
- the admin-gated synthetic-account reaper status contract, backed by a real
  server-side TTL/janitor for `agent-e2e-` labels; and
- observable backend and worker build identity that can be matched to the
  candidate commit.

Trigger **API-key deployed-runtime qualification** manually from the protected
`test` branch in GitHub Actions and enter the full intended deployment commit
SHA. Leave `runtime_target=deployed_current` to test today's deployed runtime;
select `hosted_resident` only for the strict future-V2 proof. Any other selected
ref explicitly fails before the protected Environment or its secrets are
reached. Manual mode is intentional for the first stabilization phase; there is
no push, schedule, or deployment trigger yet.

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
assertions, evidence codes, required IDs, and preserved attempt history; pre/post
endpoint liveness is proven, with strict V2 and candidate identity required only
when that target is selected; all chat turns have the
five required trace stages and numeric per-turn stage timing; cleanup succeeds;
each worker has a completed qualification-tool event and a valid, passing,
result-bound P0-12 receipt; each agent-driven scenario P0-02 through P0-11 has
its own completed command marker; required files exist; and the redaction scan
is clean. A blocked prerequisite is useful evidence, but it is never a release
PASS.
