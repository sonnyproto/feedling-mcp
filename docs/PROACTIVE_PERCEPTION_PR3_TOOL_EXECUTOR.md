# Proactive Perception PR3: Tool Catalog And Executor V2

## Scope

PR3 implements the shared tool execution contract for the V2 runtime without
cutting over hosted or resident production paths.

Source references:

- `docs/PROACTIVE_PERCEPTION_SPEC_V2.md` §2 and §6.
- `docs/PROACTIVE_PERCEPTION_ROUND3_EXECUTION_PLAN.md` PR3.

## Implementation

- `backend/proactive/tool_catalog_v2.py`
  - Keeps `DEFAULT_TOOL_SPECS_V2` as the single catalog source.
  - Adds `signature()` for stable source comparison.
  - Adds `tool_catalog_v2_for_runtime("hosted"|"resident")`, both returning
    the same V2 catalog contract.
- `backend/proactive/tool_executor_v2.py`
  - Adds `ToolExecutorV2`, `ToolCallV2`, `ToolResultV2`, `ToolTraceV2`.
  - Adds `ToolRuntimeAdaptersV2` for injected output/read adapters.
  - Records tool traces with name, cost class, latency, outcome, wake id, and
    turn id.
  - Enforces fast/slow budgets via explicit `needs_background` soft handoff.

## Tool Behavior

Implemented read/action layer:

- `perception.now`
- `perception.location`
- `perception.calendar`
- `perception.now_playing`
- `perception.motion`
- `perception.photo_recent`
- `memory.index`
- `memory.fetch`
- `send_message` when a hosted/resident output adapter is injected
- `sleep`
- `perception.weather`
- `perception.steps`
- `perception.sleep_last_night`
- `perception.workout`
- `perception.vitals`

Weather and health tools read the iOS encrypted snapshot state after backend
ingress decrypts it; absent or stale fields return `null` values rather than
fake data.

Cataloged but not implemented in PR3:

- `screen.read`
- `screen.recent`
- `schedule_wake`
- `cancel_wake`

Those remain in the catalog so the agent sees the full contract, but the PR3
executor returns an explicit unavailable result until the relevant PR wires the
actual adapter.

## Non-Goals

- No hosted or resident production cutover.
- No real LLM/tool loop.
- No HealthKit server-side placeholders.
- No direct chat write unless `send_message` is supplied by the caller.
- No deletion of existing hosted/resident execution paths.

## Known Spec Gap To Audit

`perception.photo_recent` reads through the existing `perception.service`
read surface. PR3 did not remove the older photo ingest hard-block code because
PR3 only established the shared tool execution contract; PR6b removes that
hard block while adding the iOS photo contract fixtures.

## Verification

Focused tests:

```sh
python3 -m pytest tests/test_proactive_tool_executor_v2.py -q
python3 -m py_compile backend/proactive/tool_catalog_v2.py backend/proactive/tool_executor_v2.py
git diff --check
```

Local result on 2026-06-19:

- `tests/test_proactive_tool_executor_v2.py`: 8 passed.
- `py_compile`: passed.
- `git diff --check`: passed.
