# Proactive Perception PR7: Ingress Cutover

## Scope

PR7 adds the V2 perception ingress path behind a per-user rollout flag:
`perception_ingress_runtime_v2_enabled` (default `false`).

- `/v1/perception/report` calls `service.ingest_snapshot_v2()` only when the
  flag is enabled; otherwise it keeps calling legacy `service.ingest_snapshot()`.
- Photo ingest emits `photo_added` through `PerceptionDifferV2` only when the
  flag is enabled; otherwise it keeps the legacy `photos` wake/debounce path.
- Device events route coarse mechanical events through `PerceptionDifferV2`
  (`safe_screen_phash`, `unlock_after_absence`) only when the flag is enabled;
  otherwise `/v1/device/events` only records the device event as before.
- Generated V2 differ events are written to the legacy proactive job queue only
  as a compatibility output during the strangler migration.

## Boundaries

- The PR6b iOS report fixtures remain the source contract.
- Encrypted iOS signals with `{key,envelope,changed}` are accepted, but they do
  not become anchor/motion/calendar/playback observations until an
  enclave/decrypt adapter supplies plaintext values. This avoids inventing
  WiFi/place data from ciphertext.
- `focus` remains pull/presence-only and does not revive `user_state/away`.
- The old `ingest_snapshot()` function remains the dormant production fallback
  while `perception_ingress_runtime_v2_enabled=false`. Rollback is a user-level
  flag toggle, not a code revert.
- pHash is mechanical: `safe_screen_phash` only routes to `screen_phash` while
  broadcast is `on`/`broadcasting`; broadcast off/paused produces no
  `scene_change`.

## Wake Output

Every generated wake has:

- `change_digest`
- `origin_refs`
- `presence_hints` when supplied by `PerceptionDifferV2`

During this PR the output is a legacy proactive job with those V2 fields copied
onto the job. Hosted V2 runtime consumes that job through
`wake_event_v2_from_legacy_job()`, which now preserves `presence_hints`.

## Rollout / Rollback

- Flag: `perception_ingress_runtime_v2_enabled`
- Location: hosted model API runtime profile, next to
  `hosted_wake_runtime_v2_enabled`
- Default: `false`
- Fallback: legacy `/report` ingest, legacy photo wake, and no perception V2
  side-effect from `/v1/device/events`
- Deletion of the legacy ingress path is intentionally left for a later
  cutover PR after flagged rollout metrics are reviewed.

## Verification

Focused tests:

```sh
python3 -m pytest tests/test_perception_ingress_v2.py tests/test_perception.py tests/test_ios_perception_contract_v2.py tests/test_proactive_runtime_v2.py tests/test_proactive_observability_v2.py tests/test_proactive_tool_executor_v2.py -q
python3 -m py_compile backend/perception/ingress_v2.py backend/perception/service.py backend/perception/routes.py backend/proactive/adapters_v2.py backend/proactive/routes.py
git diff --check
```
