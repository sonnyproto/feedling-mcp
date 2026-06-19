# Proactive Perception PR2: V2 Controls

## Scope

PR2 implements the Round 3 control split from
`docs/PROACTIVE_PERCEPTION_SPEC_V2.md` §8 and
`docs/PROACTIVE_PERCEPTION_ROUND3_EXECUTION_PLAN.md` PR2.

The V2 switches are:

- `ambient`: Wake-layer control for self-initiated sources.
- `scheduled`: Wake/capability control for agent-owned timers.
- `reminders_delivery`: Delivery-layer control for visible buzz/push.

## Implementation

- `backend/proactive/controls_v2.py`
  - Defines the V2 settings shape and resolver.
  - Maps old `enabled` to `ambient` only at the compatibility boundary.
  - Maps old `dnd` to `reminders_delivery=false` only at the compatibility
    boundary.
  - Ignores old `user_state` / `ai_state` as V2 gate inputs.
  - Provides wake, delivery, and scheduled-action decisions.
- `backend/proactive/runtime_v2.py`
  - `RuntimeSpineV2.submit()` now evaluates the wake gate before enqueue.
  - Accepted wakes are enriched with the resolved three-switch state.
  - `MergedWakeContextV2` always exposes `switches`.
- `backend/proactive/store_v2.py`
  - Adds `DBProactiveSettingsStoreV2`.
  - Persists new settings under `proactive_settings_v2`.
  - Reads old `proactive_settings` only as a fallback migration source.

## Semantics

- Ambient off blocks only self-initiated sources:
  `heartbeat`, `perception_event`, `scene_change`.
- Ambient off does not block:
  `user_message`, manual wakes, `scheduled_wake`, `background_result`.
- Scheduled off blocks timer execution and `schedule_wake` / `cancel_wake`
  actions with an explicit `scheduled_disabled` decision. This is not silent
  loss; callers can surface a transparent explanation.
- Delivery off does not block chat writes. It blocks visible delivery/buzz and
  keeps `reminders_delivery=false` visible in turn context.
- Manual wake bypasses wake/delivery silencing, but does not bypass the
  Scheduled capability switch for creating timers.

## Non-Goals

- No iOS UI.
- No hosted/resident production cutover.
- No new behavior added to old `dnd` / `away` suppression paths.
- No second judgment gate.

## Deferred PR8 Alignment Points

- PR8 must not ignore `WakeControlDecisionV2.transparency_required`. The
  scheduler path should either consume the `submit()` decision synchronously or
  reroute rejected timer work into an explicit transparency wake/notice. The
  final carrier belongs in PR8, not PR2.
- Product/spec decision still needed: if a user sets a timer, then turns
  `scheduled` off before it fires, should the system emit a non-buzz
  transparency turn, mark pending timers disabled at switch-off time, or stay
  fully silent? PR8 should pin this before live scheduled wake wiring.

## Verification

Focused tests:

```sh
python3 -m pytest tests/test_proactive_runtime_v2.py -q
python3 -m pytest tests/test_proactive_store_v2.py -q
python3 -m py_compile backend/proactive/controls_v2.py backend/proactive/runtime_v2.py backend/proactive/store_v2.py
git diff --check
```

Local result on 2026-06-19:

- `tests/test_proactive_runtime_v2.py`: 19 passed.
- `tests/test_proactive_store_v2.py`: 7 skipped locally because no
  `DATABASE_URL` test Postgres is configured.
- `py_compile`: passed.
- `git diff --check`: passed.
