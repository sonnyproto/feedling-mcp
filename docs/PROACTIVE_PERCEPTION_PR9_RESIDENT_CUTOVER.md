# Proactive Perception Runtime V2 - PR9 Resident Cutover

PR9 moves the resident proactive consumer onto the same V2 wake context and tool
catalog as hosted runtime while keeping the old resident executor as a dormant
rollback path.

## Cutover Flag

- Per-user flag: `resident_runtime_v2.resident_wake_runtime_v2_enabled`.
- Default: `false`.
- `/v1/proactive/jobs/poll` returns the profile at the top level and attaches it
  to every pending job under `job.runtime_v2`.
- Flag off: resident keeps using `_message_for_proactive_job`.
- Flag on: resident uses `_message_for_proactive_job_v2`; the old prompt helper
  is unreachable for that job.

## Shared V2 Context

Resident converts each compatibility job through the shared adapter:

1. `wake_event_v2_from_legacy_job()`
2. `merge_wakes_v2(..., tool_catalog=tool_catalog_v2_for_runtime("resident"))`
3. `build_agent_context_v2()`

`tool_catalog_v2_for_runtime("resident")` and
`tool_catalog_v2_for_runtime("hosted")` intentionally return the same catalog
signature. Resident-only wake semantics are not introduced.

Flag-on V2 resident turns do not inject legacy screen text or image attachments;
screen perception must be pulled through the shared tool contract.

## Resident Lease Recovery

The legacy resident job queue did not have a real lease. PR9 adds a conservative
reclaimer at `/v1/proactive/jobs/poll`:

- Reclaims `claimed` / `realizing` resident jobs after
  `RESIDENT_WAKE_LEASE_SEC`.
- Resets them to `pending` with
  `status_reason=resident_stale_claim_recovered`.
- Sets `consumer_id=recovered:<old-consumer>` so stale workers cannot later
  complete the job unnoticed.
- Does not reclaim hosted consumers (`hosted_runtime`, `hosted_runtime_v2`).

Status updates now reject a mismatched non-empty `consumer_id` with
`consumer_mismatch`.

## Scheduled Action Compatibility

Resident V2 action output may include `schedule_wake` / `cancel_wake`. PR9 adds
`POST /v1/proactive/scheduled/actions` so resident can pass those actions to the
durable PR8 scheduler. Any wake emitted by the scheduler is projected back to
the temporary compatibility queue with `legacy_job_from_wake_event_v2()`.

This endpoint is compatibility glue. The canonical scheduled state remains
`proactive_scheduled_wakes_v2`, not legacy job status.

## Verification

Focused checks:

```bash
python3 -m pytest tests/test_chat_resident_consumer.py tests/test_proactive_jobs.py tests/test_hosted_wake_v2_cutover.py tests/test_proactive_scheduled_wake_v2.py -q
python3 -m py_compile tools/chat_resident_consumer.py backend/proactive/routes.py backend/proactive/adapters_v2.py backend/proactive/resident_runtime_v2.py backend/hosted/wake_consumer.py backend/core/store.py
```
