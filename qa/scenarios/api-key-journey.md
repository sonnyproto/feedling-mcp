# API-Key Runtime V2 P0 Journey

Run every scenario below, in order, for every profile in
`qa/coverage-lock.json`. The same fresh synthetic account and content keypair are
used from `P0-02` through `P0-13`. A later scenario MUST NOT conceal an earlier
failure.

For every scenario record: start/end time, attempt count, status, fixed
`evidence_codes`, request/turn/trace identifiers, relevant latency, and either
`failure: null` or a failure object containing fixed `stage_code` and
`failure_code` values from the result schema. Profile-level diagnostics use only
fixed `diagnostic_codes`. Never record prose, credentials, or raw model/trace
content in any of those fields.

`qa/coverage-lock.json` is the machine-readable contract for the exact assertion
names, evidence codes, identifier minima, and turn counts below. Copy those
fields exactly, preserve `P0-01` through `P0-13` order, and include one numbered
`attempt_results` row per attempt. A retry never replaces its first observation.

## P0-01 — Test-target and credential preflight

**Act**

- Confirm the base URL is the designated test endpoint, never production.
- Confirm the expected deployment SHA and the provisioner's private manifest are
  present. Verify that this profile has sanitized successful receipts for
  registration, invalid-key rejection, valid-key recovery, trace enablement, and
  Runtime V2 readback. Provider and admin secrets MUST NOT be present in the
  agent environment.
- Confirm all five contract files are readable and JSON inputs parse.

**Pass**

- Target is test, the deployed candidate can be identified, and every required
  prerequisite for this profile is present.

**Classify**

- Missing or failed provisioner receipt: `BLOCKED_CREDENTIAL`.
- Wrong or unverifiable candidate deployment: `BLOCKED_DEPLOYMENT`.
- Production/unsafe target: `SECURITY_FAIL`.

## P0-02 — Fresh model-API account onboarding

**Act**

- Load this profile's synthetic account session from the private manifest.
- Verify `whoami`, the generated `agent-e2e-<run-id>-<profile-id>` label, and
  the provisioner's registration/fresh-state receipt.
- Clear the account's user-owned debug trace before semantic scenarios begin.

**Pass**

- A new synthetic user ID and Feedling credential are returned; `whoami` resolves
  to that same user; no previous identity, memories, or chat are present.

## P0-03 — Invalid provider-key rejection

**Act**

- Audit the provisioner's invalid-key request receipt and bounded error evidence.
- Confirm it predates the valid setup receipt and used this profile's provider,
  model, and optional base URL.

**Pass**

- Setup rejects the fake key with a bounded actionable error, does not report the
  account as configured, does not echo the key, and does not start hosted chat.

## P0-04 — Valid provider-key recovery and validation

**Act**

- Audit the provisioner's valid-key setup receipt on the same account.
- Verify the public/masked configuration after setup without requesting or
  reading the provider credential. For `relay-kongbeiqie`, require provider
  `openai_compatible` and exact equality between the private manifest's
  `configured_base_url` and `valid_key_receipt.base_url`; never copy that endpoint
  into a public result or diagnostic.

**Pass**

- Setup reports configured/validated for the exact provider and model; the prior
  invalid attempt does not poison recovery; no response or artifact contains the
  credential.

## P0-05 — Runtime V2 selection and independent verification

**Act**

- Audit the provisioner's test-control-plane set and independent readback
  receipts for `db_action_v2`.
- Capture candidate backend and worker identity when exposed.

**Pass**

- Expected and observed runtime both equal `db_action_v2`; deployment identity
  matches the expected candidate; the V2 worker pool reports live/readiness
  evidence.

## P0-06 — Persona-file import and distillation

**Act**

- On this same account, submit all four material classes from
  `qa/fixtures/persona-import-v1.json`: chat history, AI persona, personal profile,
  and memory summary.
- Use `tools/genesis_e2e.py distill-existing-session` with this profile's
  one-row `QA_PRIVATE_MANIFEST`, a `0600` evidence path beneath its isolated
  `TMPDIR`, and `QA_ARTIFACT_DIR` only as the denied public-artifact boundary.
  The agent cannot read or write that directory. Do not provision a second
  user, consume a provider secret, or replace/delete the provisioned provider
  configuration.
- After capture reaches `done`, read the decrypted private evidence and write a
  separate owner-mode `0600` semantic judgment containing exactly
  `schema_version: 1`, `judge: qualification_agent`, the capture's exact
  `evidence_sha256`, all three reviewed surfaces, the exact locked fact IDs,
  and true/false consistency, support, and contradiction decisions.
- Run `distill-existing-session-finalize` with the fixture, evidence, judgment,
  and artifact boundary. It must verify the hash binding, sanitize its bounded
  result, and delete the plaintext evidence. Any optional helper report must
  remain beneath private `QA_WORK_ROOT` or `TMPDIR`; only the trusted renderer
  may later create public artifacts. Lexical matches are extraction evidence
  only and can never produce PASS by themselves. Record bounded check/evidence
  codes, never decrypted content or the forbidden fixture value.
- Bind the canonical scenario evidence to that exact finalizer receipt. The
  scenario's sole `request_id` must equal `persona_finalizer.request_id`, and
  `persona_finalizer` must record fixture `persona-import-v1`, the verified
  lowercase SHA-256, finalizer `job_id`, `semantic_judgment_bound: true`,
  `finalizer_ok: true`, `private_evidence_deleted: true`, and
  `privacy_violation_count: 0`. Do not copy this receipt to any other scenario.

**Pass**

- Import finishes once, identity name/category/dimensions/self-introduction are
  populated, all locked ground-truth facts are represented without duplicates,
  the relationship anchor exists, the onboarding validator passes, and the
  privacy-firewall value is absent from identity fields, persona output, and
  self-introduction.

## P0-07 — Hosted activation and live-loop verification

**Act**

- Enable the model-API driver.
- Run `chat/verify_loop` before the first real user message.

**Pass**

- The selected driver is returned, verification reaches `passing=true`, Runtime
  V2 remains selected, and no orphan user turn is created during verification.

## P0-08 — Basic chat and acknowledgement latency

**Act**

- Send one text turn containing a unique run/profile nonce and request an exact
  nonce echo.
- Measure acknowledgement and end-to-end reply latency; decrypt the reply.

**Pass**

- Send returns the hosted asynchronous contract, exactly one correlated agent
  reply arrives before timeout, the nonce is present, and the reply is not a
  fallback/error response.

## P0-09 — Ten-turn delivery reliability

**Act**

- Execute ten sequential turns with unique turn nonces. Do not rely on
  `provider_smoke --turns 10`; explicitly send and correlate all ten turns.
- Include a token introduced on turn 1, distractors on intermediate turns, and a
  request to repeat the original token on turn 10.

**Pass**

- Ten sends produce ten and only ten correctly ordered correlated replies; no
  turn is missing, duplicated, fallback, or attributed to another turn; turn 10
  recalls the original token.

## P0-10 — Context, imported memory, and persona consistency

**Act**

- Turn 1 asks about both the canonical user preference and shared event from the
  fixture.
- Turn 2 asks the agent to respond to an ordinary emotional prompt in its
  imported style.
- Compare the answer semantically with the decrypted identity, memories, and
  persona rather than requiring brittle exact wording.

**Pass**

- Locked facts are recalled without inventing contradictory facts; the response
  preserves the fixture's agent identity and style across the conversation.

## P0-11 — Model and agent self-identification

**Act**

- Ask who the companion is and which provider/model route is serving the turn.
- Compare the answer with the configured persona and locked profile, and compare
  the route claim with trace/config evidence.

**Pass**

- Companion identity matches the imported persona; provider/model family does not
  contradict the configured profile; authoritative trace/config identifies the
  exact provider, model, and Runtime V2 path. A plausible reply alone is not
  sufficient route evidence.

## P0-12 — Reasoning metadata and user-visible disclosure

**Act**

- Send a small deterministic reasoning task that has an objectively checkable
  final answer.
- Verify the correlated route and harness expose reasoning capability as enabled.
  Record requested, configured, and effective effort separately; all three must
  be `medium`. A configured route value alone is insufficient if the harness or
  selected model clamps effective effort to `off`.
- Inspect the correlated chat record and trace for reasoning kind/source/model,
  token metadata, at least one provider-visible reasoning/thinking event, and the
  separately encrypted user-visible disclosure.
- Decrypt only to assert nonempty/sanitized client-visible content; do not persist
  its text.
- Copy the sole P0-12 turn's exact `request_id`, `turn_id`, and `trace_id` into
  the profile's bounded `reasoning` evidence object. Reasoning evidence from a
  different request, turn, or trace is not valid even when its metadata looks
  correct.

**Pass**

- Final answer is correct; capability is enabled; requested, configured, and
  effective effort are all `medium`; the correlated turn has a positive
  provider-visible reasoning/thinking event count; required reasoning and token
  metadata are present; user-visible disclosure decrypts and is nonempty;
  artifacts contain only metadata/counts/length. Provider-visible reasoning or a
  provider-produced summary is the contract; hidden raw chain-of-thought is
  neither promised, requested, nor stored.

If the profile requested and configured `medium` but the trace reports
`capability_enabled: false`, `effective_effort: off`, or zero reasoning events,
record those observed values verbatim, classify P0-12 as `PRODUCT_FAIL` at stage
`REASONING` with `REASONING_EFFORT_CLAMPED`, and preserve the sanitized failure
artifact. Never replace a failed observation with success-shaped defaults.

## P0-13 — Trace completeness, latency attribution, and cleanup

**Act**

- Read the final user-owned trace and correlate evidence to this profile's turn
  and trace IDs.
- Record available queue, model/provider, persistence, delivery, acknowledgement,
  and total durations; explicitly list unavailable stages.
- Capture sanitized evidence, disable/delete provider hosting, reset this synthetic
  account using `{"confirm":"delete-all-data"}`, and verify the old Feedling
  credential is rejected.

**Pass**

- Trace is enabled and every tested chat turn correlates the exact stages
  `routing`, `queue`, `provider`, `persistence`, and `delivery`; every PASS-stage
  latency is numeric and no stage is missing; cleanup succeeds and the old
  account credential no longer authenticates.

**Classify**

- Correct user behavior but unavailable required trace: `BLOCKED_EVIDENCE`.
- Reproducible missing/duplicate stage or cleanup endpoint failure:
  `PRODUCT_FAIL`.
- Worker omitted cleanup or produced malformed evidence: `AGENT_ERROR`.
