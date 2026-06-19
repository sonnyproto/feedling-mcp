# Proactive Perception PR8: Scheduled Wake

## Scope

PR8 implements agent-owned durable timers:

- `schedule_wake` creates a persistent timer with wall-clock `at`, `tz`, note,
  origin refs, and derived `due_at`.
- `cancel_wake` cancels pending timers by wake id.
- Due timers are claimed with a reclaimable lease before firing, so multiple
  workers cannot double-fire the same timer.
- Fired timers become `scheduled_wake` events and enter the Runtime V2 wake
  inbox through the same compatibility boundary as other strangler paths.

## Product Decisions Pinned

- Pending cap starts at 20 per user. The agent context exposes
  `scheduled_wakes.pending_count` and `scheduled_wakes.pending_cap`.
- If the cap is exceeded, the oldest pending timer is evicted and the
  `schedule_wake_result` includes `evicted_timer_ids`.
- If a pending timer becomes due while Scheduled is off, the timer is marked
  `blocked/scheduled_disabled` and a `background_result` transparency wake is
  submitted with the original note and origin refs. The blocked timer is not
  retried automatically.
- If an agent emits `schedule_wake` or `cancel_wake` while Scheduled is off, the
  action executor records a rejected result and consumes
  `transparency_required` by submitting a transparency wake. It does not
  silently drop the action.

## Hosted Cutover Boundary

Hosted tick now runs the scheduled due pass before heartbeat legacy
enabled/dnd handling. Scheduled wake behavior is still guarded by the hosted V2
operations flag: if `hosted_wake_runtime_v2_enabled=false`, pending V2 timers
stay pending rather than being sent to the legacy wake executor.

During the strangler migration, due timer events are projected to compatibility
`proactive_jobs`; hosted/resident consumers then adapt them into `WakeEventV2`
and drain the Runtime V2 inbox.

## Verification

Focused tests:

```sh
python3 -m pytest tests/test_proactive_scheduled_wake_v2.py tests/test_proactive_runtime_v2.py tests/test_proactive_agent_protocol_v2.py tests/test_hosted_wake_v2_cutover.py -q
python3 -m py_compile backend/proactive/scheduled_wake_v2.py backend/proactive/runtime_v2.py backend/hosted/wake_consumer.py
git diff --check
```
