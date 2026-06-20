# Proactive Perception Round 3 PR1: DB-Backed Runtime Substrate

## Scope

This PR adds the persistent V2 substrate only:

- DB-backed wake inbox (`proactive_wakes_v2` stream)
- DB-backed foreground turn leases (`proactive_v2_lease:turn:*` blobs)
- DB-backed background leases (`proactive_v2_lease:background:*` blobs)
- DB-backed turn records (`proactive_turns_v2` stream)

It does not connect hosted or resident production execution.

## Spec Alignment

- `PROACTIVE_PERCEPTION_SPEC_V2.md` §1.4 / D11:
  - per-user single-flight is represented by foreground turn leases.
  - wake inbox persists wakes before they become a turn.
  - merge semantics stay in runtime (`RuntimeSpineV2` / `merge_wakes_v2`).
  - background leases are separate from foreground turn leases.
- `PROACTIVE_PERCEPTION_ROUND3_EXECUTION_PLAN.md` PR1:
  - V2 state uses new V2 streams / blobs, not legacy `proactive_jobs.status`.
  - lease acquisition is DB CAS through `user_blobs`.
  - stale `running` turns can be recovered without letting old owners complete.

## Storage

- `proactive_wakes_v2`: append-only wake records keyed by `wake_id`.
- `proactive_turns_v2`: append-only turn records keyed by `turn_id`.
- `proactive_v2_lease:<scope>`: current lease blob for each user/scope.

Legacy `proactive_jobs` remains untouched and is not a source of truth for V2.

## Acceptance Coverage

Implemented in `tests/test_proactive_store_v2.py`:

- two workers racing for the same user: only one foreground lease wins.
- expired foreground lease is reclaimable.
- expired background lease is reclaimable.
- releasing an old/replaced lease fails.
- persisted wake inbox round-trips and merges wakes.
- latency-sensitive persisted wake flushes without waiting for merge window.
- stale `running` turn recovery prevents old owner from starting/completing.

## Local Verification

On machines without a reachable test Postgres, `tests/test_proactive_store_v2.py`
skips by design. CI / reviewers with `FEEDLING_TEST_PG` or the default local
test Postgres should run it against the real DB fixture.

Commands run locally:

- `python3 -m pytest tests/test_proactive_runtime_v2.py tests/test_proactive_store_v2.py -q`
- `python3 -m py_compile backend/proactive/runtime_v2.py backend/proactive/store_v2.py backend/proactive/tool_catalog_v2.py backend/proactive/adapters_v2.py backend/perception/differ_v2.py tests/test_proactive_store_v2.py`
- grep audit for legacy terms across V2 runtime/store/tests.

Local result: `11 passed, 6 skipped` because no local test Postgres is available.
