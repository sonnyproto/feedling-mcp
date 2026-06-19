# PR5: Background Slow Path

## Scope

PR5 adds the V2 background slow-path lifecycle without cutting over hosted or
resident production entrypoints.

It adds:

- `backend/proactive/background_v2.py`
- In-memory background job store for contract tests
- `BackgroundWorkerV2` with explicit claim/run/complete lifecycle
- Optional `background_jobs` integration on `TurnRunnerV2`
- DB-backed `proactive_background_jobs_v2` stream
- Background completion as `background_result` wake inbox re-entry

## Spec Alignment

- Spec §1.4 / D11: background work does not hold the foreground turn slot.
- Spec §1.4: background completion never writes chat directly; it submits a
  `background_result` wake.
- Spec §6 / Appendix scenario 8: late results are arbitrated by the agent after
  seeing current chat, so stale results can be folded or dropped.

## Runtime Decisions

- Foreground `TurnRunnerV2` creates a pending background job when a job store is
  configured. The background worker acquires the background lease when it
  actually runs the job.
- Existing no-job-store behavior is retained for earlier contract tests: it can
  still return a background lease immediately.
- `BackgroundWorkerV2` exposes no chat or push adapter. Its only successful
  output path is `RuntimeSpineV2.submit(WakeEventV2(source="background_result"))`.
- `background_payloads` are included in the agent context even when a
  latency-sensitive `user_message` is the primary trigger. This is not passive
  perception injection; it is the explicit result of earlier background work
  that the agent must arbitrate against current chat.
- Duplicate completion is guarded by job status + lease id. A second completion
  cannot emit a second `background_result` wake.

## Non-Goals

- No hosted wake cutover.
- No resident cutover.
- No daemon/thread runner.
- No direct chat/push delivery implementation.
- No PR7 perception ingress work.

## Tests

Run:

```sh
python3 -m pytest tests/test_proactive_background_v2.py tests/test_proactive_agent_protocol_v2.py tests/test_proactive_runtime_v2.py tests/test_proactive_tool_executor_v2.py tests/test_proactive_store_v2.py -q
python3 -m py_compile backend/proactive/background_v2.py backend/proactive/agent_protocol_v2.py backend/proactive/runtime_v2.py backend/proactive/store_v2.py
git diff --check
```

Current local result:

- `39 passed, 9 skipped`
- `py_compile` passed
- `git diff --check` passed
