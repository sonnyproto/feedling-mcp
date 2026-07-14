# Persona and memory regression

This package is a dependency-light release regression harness for Feedling
companions. It does not add LangChain, DeepEval, or a hosted eval SDK. It reuses
the repository's synthetic-account manifests, encrypted chat transport,
deployment identity receipt, cleanup path, and provider client.

The contract borrows the useful, portable parts of established eval systems:

- DeepEval-style multi-turn cases become versioned `Scenario`, `Turn`, and
  `Trajectory` records.
- LangSmith-style datasets and experiments become a checked-in Golden Persona,
  fixed scenarios, target descriptors, private experiment results, and a
  baseline/candidate comparison.
- Deterministic assertions and model-judged semantics stay separate. A judge or
  evidence failure is `BLOCKED_EVIDENCE`, never a product failure.

## What is locked

The Golden Persona fixes identity, role, tone, behavioral invariants,
relationship facts, and memory facts without requiring identical wording. Its
source hash transitively covers the persona-import manifest and all four raw
material files. Scenarios, rubrics, evaluator algorithms, runner evidence
version, repetition count, and metric catalog are also fingerprinted.

Baseline and candidate must be different target IDs and different full build
SHAs. Both builds must use the same runtime mode, provider, model, reasoning
configuration, persona, scenarios, judge configuration, and coverage contract.
Changing the measuring instrument blocks comparison and requires a deliberate
new baseline. Covered live results must also carry distinct deployment-receipt
digests and disjoint hashed account batches; missing, reused, or overlapping
identity evidence blocks the gate.

Experiment JSON contains plaintext prompts and replies and is therefore a
private mode-0600 artifact. It is no-overwrite but is not cryptographically
signed; an approved-baseline digest and retention policy remain a CI/release
responsibility. Public JSON, Markdown, and JUnit reports contain only allowlisted
aggregate fields and fixed failure codes—no messages, memories, canaries,
rationales, evidence turn IDs, account IDs, or credentials.

## Release coverage

The release lane currently includes:

- short persona override resistance;
- imported memory after transcript clearing;
- false-memory contradiction resistance;
- private import-canary protection;
- a ten-turn mixed persona/memory stress conversation;
- cross-user memory isolation using two accounts; and
- honesty about an unknown shared event.

Deterministic hard gates cover narrow facts that code can establish: exact
identity/role claims, question/length limits, fact markers in one complete probe
turn, privacy canaries, and narrowly configured contradictions. A structured
judge covers tone, natural recall, long-horizon consistency, contradiction
handling, cross-user behavior, and unsupported-memory honesty. Judge requests
are blind to baseline/candidate labels and omit experiment, account, session,
request, response, and trace identifiers.

Soft metric regressions remain visible in the matrix but do not fail JUnit or
the release status. Hard metric regressions, invalid evidence, infrastructure
errors, and contract mismatches do.

## Memory evidence levels

- Same-session turns prove only context retention.
- `clear_history` proves a transcript boundary. It does not prove that the
  underlying model runtime session rotated.
- `rotate_runtime_session` requires deployment-specific before/after runtime
  evidence. The protocol and nightly fixture support this distinction, but the
  current live CLI has no production rotator and therefore refuses that nightly
  scenario rather than making a false persistence claim.

## Commands

Validate all checked-in contracts without network access:

```bash
python qa/run_persona_memory_regression.py validate
```

Run one real target. Repeat `--manifest-profile` for every isolated account
reported as required by the CLI. All manifests must describe the same route and
must already be fresh, chat-gated, and imported with the locked persona bundle.
The deployment receipt must be produced by `qa/verify_deployment.py` under the
serialized test-deployment lock no more than 30 minutes before this command.

```bash
python qa/run_persona_memory_regression.py run-live \
  --target-id candidate-<full-sha> \
  --target-label candidate \
  --build-sha <40-or-64-char-lowercase-sha> \
  --deployment-receipt /private/deployment-receipt.json \
  --manifest-profile eval-001=/private/eval-001.json \
  --manifest-profile eval-002=/private/eval-002.json \
  --accounts-ready \
  --cleanup-account \
  --repetitions 3 \
  --concurrency 3 \
  --judge-provider openai \
  --judge-model <pinned-judge-model> \
  --judge-base-url https://api.openai.com/v1 \
  --judge-api-key-env QA_EVAL_JUDGE_API_KEY \
  --judge-id persona-memory-judge-v1 \
  --judge-configuration-id rubric-prompt-v1 \
  --allow-private-judge-egress \
  --output /private/candidate-result.json
```

`ProviderClientJudge` reuses `backend.provider_client` and the dependencies
already installed by this repo. A private service implementing the generic
hash-bound JSON contract can be used with `--judge-endpoint` instead.

Run baseline and candidate independently against fresh, disjoint account
batches, then compare their private results and publish allowlisted reports:

```bash
python qa/run_persona_memory_regression.py compare \
  --baseline /private/baseline-result.json \
  --candidate /private/candidate-result.json \
  --output-dir artifacts/persona-memory
```

## Operational boundary

`run-live` concurrently starts isolated conversation sessions; it does not
create Linux containers. It intentionally does not yet provision a same-route
account pool or run the Persona/Genesis import itself. Reuse the existing
`provision_profiles.py` and `genesis_e2e.py distill-existing-session` workflow
to prepare accounts, and treat `--accounts-ready` as an operator attestation
until a bundle-bound machine-readable readiness receipt is added. Never reuse
the same account batch across baseline and candidate.

For a production release gate, bracket the run with the repository's existing
pre/post deployment identity verification so a redeploy during evaluation
cannot mix builds. Start in a non-blocking CI job, calibrate semantic thresholds
against human labels, and only then make the hard-gate comparison required.

References: [DeepEval multi-turn test cases](https://deepeval.com/docs/evaluation-multiturn-test-cases),
[DeepEval conversation simulator](https://deepeval.com/docs/conversation-simulator),
[LangSmith evaluation](https://docs.langchain.com/langsmith/evaluation), and
[LangSmith experiment comparison](https://docs.langchain.com/langsmith/compare-experiment-results).
