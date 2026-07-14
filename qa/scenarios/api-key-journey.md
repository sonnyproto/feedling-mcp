# API-Key Deployed-Runtime P0 Journey

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

For each agent-driven live scenario P0-02 through P0-11, at least one real probe
command MUST begin with the exact environment assignment
`QA_SCENARIO_ID=P0-XX ` for that scenario. The trusted launcher consumes these
markers from completed Codex events without retaining command text. A comment,
an uncompleted command, or one command containing several marker strings does
not prove scenario execution. P0-01, P0-12, and P0-13 have separate
parent-owned evidence and do not use these markers.

P0-06 is the exception to the one-command minimum: it requires exactly three
ordered, successful tool calls prefixed with `QA_SCENARIO_ID=P0-06` and distinct
`QA_SCENARIO_PHASE=CAPTURE`, `REVIEW`, and `FINALIZE` assignments, using the
exact commands embedded in the profile-agent prompt. CAPTURE and FINALIZE
directly invoke the checked-in Genesis tool against the fixed private evidence
path `$QA_WORK_ROOT/p0-06-private-evidence.json`; REVIEW directly reads that
file in its own tool call and aborts if the fixed judgment path already exists.
Only after observing REVIEW output may the agent write its own semantic judgment to the fixed
`$QA_WORK_ROOT/p0-06-semantic-judgment.json` path and invoke FINALIZE. Never
generate a script that derives an all-true judgment from `expected_fact_ids`
without reviewing the decrypted evidence surfaces.

## P0-01 — Test-target and credential preflight

**Act**

- Confirm the base URL is the designated test endpoint, never production.
- Confirm the candidate SHA and the provisioner's private manifest are
  present. Verify that this profile has sanitized successful receipts for
  registration, invalid-key rejection, valid-key recovery, trace enablement, and
  authenticated runtime readback. Provider and admin secrets MUST NOT be present in the
  agent environment.
- Confirm all five contract files are readable and JSON inputs parse.

**Pass**

- Target is test, the deployed endpoint is reachable, and every required
  prerequisite for this profile is present.

**Classify**

- Missing or failed provisioner receipt: `BLOCKED_CREDENTIAL`.
- Unreachable or incompatible test deployment: `BLOCKED_DEPLOYMENT`.
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

## P0-05 — Deployed-runtime discovery and readiness

**Act**

- Audit the provisioner's authenticated, user-scoped runtime readback receipt.
- Record the observed runtime mode and version without treating the backend's
  legacy version label as proof that the new Hosted Runtime V2 architecture is deployed.
- When `QA_EXPECTED_RUNTIME=hosted_resident`, additionally require mode
  `hosted_resident`, runtime version `2`, and the trusted parent-owned V2
  deployment receipts.

**Pass**

- The account is configured, runtime status is readable, and the observed mode
  and version are recorded. In strict V2 mode, all additional V2 requirements match.

## P0-06 — Persona-file import and distillation

**Act**

- On this same account, submit all four material classes from
  `qa/fixtures/persona-import-v1.json`: chat history, AI persona, personal profile,
  and memory summary.
- Use `tools/genesis_e2e.py distill-existing-session` with this profile's
  one-row `QA_PRIVATE_MANIFEST`, the fixed `0600` evidence path
  `$QA_WORK_ROOT/p0-06-private-evidence.json`, and `QA_ARTIFACT_DIR` only as the
  denied public-artifact boundary. The agent cannot read or write that
  directory. Do not provision a second user, consume a provider secret, or
  replace/delete the provisioned provider configuration.
- After capture reaches `done`, read the decrypted private evidence and write a
  separate owner-mode `0600` semantic judgment at
  `$QA_WORK_ROOT/p0-06-semantic-judgment.json` containing exactly
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

- The selected driver is returned, verification reaches `passing=true`, the
  observed runtime remains configured, and no orphan user turn is created
  during verification.

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
  exact provider, model, and observed deployed-runtime path. A plausible reply alone is not
  sufficient route evidence.

## P0-12 — Reasoning metadata and user-visible disclosure

**Act**

- Request the deterministic parent-owned delivery probe exactly once for this
  profile. The profile writes only a fixed marker, then waits for the sanitized
  facts copy:

  ```sh
  umask 077
  test ! -e "$QA_WORK_ROOT/.cot-probe-request"
  test ! -e "$QA_WORK_ROOT/cot-delivery-facts.json"
  printf '%s\n' "$QA_PROFILE_ID" > "$QA_WORK_ROOT/.cot-probe-request"
  i=0
  while test ! -f "$QA_WORK_ROOT/cot-delivery-facts.json" && test "$i" -lt 360; do
    sleep 1
    i=$((i + 1))
  done
  test -f "$QA_WORK_ROOT/cot-delivery-facts.json"
  ```

  Do not invoke `qa/cot_delivery_probe.py` yourself and do not create, replace,
  edit, or delete the facts copy. The trusted parent derives the nonce, sends
  the sole P0-12 turn, and writes the authoritative receipt under its private
  worker-output root. That root is explicitly denied to this profile.

  A facts copy containing a failed or unverified receipt is a completed
  observation, not a reason to discard it or send a replacement P0-12 turn. An
  unavailable/error facts copy is an evidence failure, not permission for the
  profile to run its own probe.
- The probe sends `17 × 19`, requires final answer `323`, exact-correlates the
  resulting user turn and stored reply, and records only bounded metadata. Do
  not send a second reasoning task or substitute a different turn.
- Verify the correlated route and harness expose reasoning capability as enabled.
  Record requested, configured, and effective effort separately; all three must
  be `medium`. A configured route value alone is insufficient if the harness or
  selected model clamps effective effort to `off`.
- Bind the profile result to the receipt's exact request/turn/trace and reply
  IDs. Inspect its bounded reasoning kind/source/model, token-metadata status,
  provider-visible reasoning event count, and separately encrypted user-visible
  disclosure result. The trusted launcher separately validates and hashes this
  receipt; agent prose is not the authority for the delivery observation.
- Copy failed/unverified receipts literally: empty request/turn/trace IDs require
  empty reasoning ID strings and empty scenario ID arrays; empty delivered
  kind/source/model strings require `null` result fields. Do not replace an empty
  delivered-thinking model with the configured provider model.
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

Map deterministic delivery failures without inventing evidence:

- `DOWNSTREAM_PARSE_DROPPED_REASONING` or invalid reasoning metadata is
  `PRODUCT_FAIL` / `REASONING_METADATA_MISSING`;
- a missing or unreadable delivered thinking envelope is `PRODUCT_FAIL` /
  `DISCLOSURE_MISSING`;
- an incorrect final answer is `PRODUCT_FAIL` / `CONTENT_ASSERTION_FAILED`;
- unavailable, ambiguous, or dropped trace—or an unobserved positive model
  reasoning signal—is `BLOCKED_EVIDENCE` with `TRACE_INCOMPLETE` or
  `TRACE_UNAVAILABLE` unless other exact correlated evidence resolves the
  ambiguity; and
- absent provider token accounting remains `BLOCKED_EVIDENCE` /
  `REASONING_TOKENS_MISSING` for release qualification. It does not erase a
  separately proven delivery PASS in the local diagnostic receipt.

## P0-13 — Trace completeness, latency attribution, and cleanup

**Act**

- Read the final user-owned trace and correlate evidence to this profile's turn
  and trace IDs.
- Record available queue, model/provider, persistence, delivery, acknowledgement,
  and total durations; explicitly list unavailable stages.
- Capture sanitized evidence, disable/delete provider hosting, reset this synthetic
  account using `{"confirm":"delete-all-data"}`, and verify the old Feedling
  credential is rejected.
- Local adminless diagnostic exception: when
  `QA_QUALIFICATION_MODE=diagnostic`, do not call account reset from the agent.
  The deterministic parent performs the sole reset after collecting the worker
  result and COT receipt; record the account-reset/old-credential assertions as
  false and cleanup as deferred. This exception can never produce a release
  PASS. Provider-config deletion may still be attempted before returning.

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
