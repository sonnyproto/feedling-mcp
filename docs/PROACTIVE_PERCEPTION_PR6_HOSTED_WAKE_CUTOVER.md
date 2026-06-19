# PR6: Hosted Wake Cutover

## Scope

PR6 cuts over hosted proactive wake execution to Runtime V2 behind a per-user
flag while retaining the old hosted executor as the rollback path.

Flag:

- `model_api_runtime.hosted_wake_runtime_v2_enabled`
- Default: `false`

When the flag is off, `_run_model_api_wake_job_inner()` delegates to
`_run_model_api_wake_job_inner_legacy()` unchanged.

If the flag cannot be read, the branch fails closed to the legacy executor.

When the flag is on:

- The legacy `proactive_jobs` row is claimed only as an input adapter.
- The job is converted to `WakeEventV2`.
- `RuntimeSpineV2` applies V2 wake controls and settings.
- `TurnRunnerV2` runs the V2 agent protocol.
- Chat write and push are output compatibility only.
- Legacy `proactive_jobs` receives terminal status projection for the old
  dashboard.

## Spec Alignment

- Spec §1 / D1: hosted wake now uses the same V2 turn protocol as other wakes.
- Spec §1.4 / D11: hosted V2 turns use the V2 turn lease and runner.
- Spec §8 / D16: V2 wake and delivery controls are separated. Delivery-off
  writes chat but suppresses push.
- Spec §7.3: agent context carries `timezone` and `local_time`, so hosted V2
  is not UTC-blind.

## Runtime Decisions

- The migration flag is not one of the three user-facing switches. It lives in
  the hosted runtime profile as an operations cutover flag.
- V2 provider prompts do not call the old wake prompt builder or old action
  parser.
- Visible delivery is emitted only from parser-normalized `outcome.messages`.
  `send_message` actions are promoted into that tuple and deduped with
  top-level `messages`, so either model form can speak without creating a
  second delivery path.
- Manual hosted wakes with no top-level message are projected as
  `ignored_manual`.
- If the V2 turn lease is busy, the legacy job is returned to `pending` for the
  reconcile pass instead of running the old executor.

## Rollback

Set `model_api_runtime.hosted_wake_runtime_v2_enabled=false` for the user. The
same entrypoint immediately uses `_run_model_api_wake_job_inner_legacy()`.

## Follow-Up Deletion PR

After the observation window and §10.3 metrics are healthy, delete the dormant
legacy hosted wake executor pieces:

- `_run_model_api_wake_job_inner_legacy`
- `model_api_runtime.wake.wake_turn_contract_message`
- `model_api_runtime.wake.build_wake_event_message`
- `model_api_runtime.wake.parse_wake_actions`
- Old hosted wake `set_ai_state` handling

Do not delete them in PR6; invariant 13 requires a live rollback path.

## Tests

Run:

```sh
python3 -m pytest tests/test_hosted_wake_v2_cutover.py tests/test_proactive_agent_protocol_v2.py tests/test_proactive_runtime_v2.py tests/test_hosted_wake_distribution.py tests/test_model_api_wake.py tests/test_proactive_store_v2.py -q
python3 -m py_compile backend/hosted/wake_consumer.py backend/proactive/agent_protocol_v2.py backend/proactive/runtime_v2.py backend/proactive/store_v2.py backend/hosted/config_store.py
git diff --check
```
