# Proactive Perception PR6b: iOS Contract Fixtures

## Scope

PR6b locks the current iOS perception producer contract before PR7 cuts
`/v1/perception/report` and photo ingest over to the V2 runtime spine.

Source iOS repo synced before fixture work:

- Path: `/Users/xiaotingtan/Desktop/feedling-mcp-ios`
- Branch: `main`
- Commit: `23d1eba54557b5f133ca720083038dd9a5d68d54`

## Source Files Audited

- `App/FeedlingTest/Pages/Settings/Perception/PerceptionContextSnapshot.swift`
- `App/FeedlingTest/Pages/Settings/Perception/PerceptionPermissionsManager.swift`
- `App/FeedlingTest/API/FeedlingAPI.swift`
- `App/FeedlingBroadcast/SharedConfig.swift`
- `App/FeedlingBroadcast/SampleHandler.swift`

## Report Envelope

iOS posts `/v1/perception/report` as:

```json
{
  "context_snapshot": [],
  "client_ts": "1781874000"
}
```

The preview-only wrapper fields `generated_at`, `source`, `upload_enabled`, and
`device_context` are not uploaded.

## Signal Mapping

| iOS key | Current shape | V2 mapping |
|---|---|---|
| `time` | `{key,data,message}`, data is JSON string | Pull-only, differ signal `time`, zero wake |
| `battery` | `{key,data,message}`, data is JSON string | Pull-only, differ signal `battery`, zero wake |
| `broadcast` | `{key,data,message}`, data is JSON string | Runtime context for broadcast regime, no direct wake |
| `focus` | `{key,data,message}`, data is JSON string or null | Pull/presence-only; must not revive old `user_state/away` gate |
| `location_signal` | `{key,envelope,changed}` or `{key,data:null,message}` | Decrypt before differ; stores coarse labels plus `wifi_anchor_id`; non-empty changed `wifi_anchor_id` feeds `wifi_anchor` differ |
| `motion_state` | `{key,envelope,changed}` or `{key,data:null,message}` | Decrypt before differ; pull-only `motion_state`, zero wake |
| `calendar_next_event` | `{key,envelope,changed}` or `{key,data:null,message}` | Decrypt before differ; calendar presence/pull context |
| `playback` | `{key,envelope,changed}` or `{key,data:null,message}` | Decrypt before differ; pull-only `now_playing`, zero wake |
| `audio_route` | `{key,envelope,changed}` or `{key,data:null,message}` | Decrypt then store pull-only `output_type` / `is_bluetooth` / `device_name`, zero wake |
| `weather` | `{key,envelope,changed}` or `{key,data:null,message}` | Decrypt then store pull-only coarse weather, zero wake |
| `health_sleep` / `health_workout` / `health_vitals` | `{key,envelope,changed}` or `{key,data:null,message}` | Decrypt then store pull-only HealthKit buckets, zero wake |
| `unsupported` | `{key,data,message}` with null subfields | Explicit ignored result |

Unknown future keys classify as `unknown_signal` / `error`; they must not
silently generate wakes.

## Fixtures

Fixtures live under `tests/fixtures/perception_ios_v2/`.

- `ios_report_full_changed.json`: all current report keys, encrypted sensitive
  signals with `changed=true`.
- `ios_report_unchanged.json`: same encrypted shape with `changed=false`.
- `ios_report_missing_permission_unavailable.json`: null data for missing
  permission and authorized-but-unavailable signals.
- `ios_report_dropped_upload.json`: content keys unavailable, so encrypted
  signals are omitted by iOS `compactMap`.
- `ios_photo_evaluate_document.json`: current photo body with `metadata`,
  `content_envelope`, and optional `meta_envelope`; no raw `exif_gps`.
- `manifest.json`: source commit, dropped-upload explanation, and human device
  verification status.

## Photo V2 Alignment

`docs/PROACTIVE_PERCEPTION_SPEC_V2.md` §2.1 says V2 removes the old
sensitive-scene hard block. PR6b updates the backend photo path accordingly:

- `document`, `id_card`, `medical`, `screenshot`, `private`, and `receipt`
  remain `sensitive=true` metadata.
- Sensitive metadata no longer rejects or discards encrypted photo content.
- Optional iOS `meta_envelope` is preserved encrypted and returned only by the
  single-photo content endpoint, not by the recent-photo list.

## HealthKit / Weather / Audio Route

HealthKit, WeatherKit, and audio-route producers now report encrypted,
already-coarsened payloads. Backend ingress accepts them as pull-only after
decrypt; they must not become wake sources.

## Manual Device Gate

Human device/simulator report verification is still pending. Agents can prepare
fixtures and tests, but only a human can certify that the iOS app hit the
backend from a device or simulator.

Status in `manifest.json`: `pending_user_verification`.

## Verification

Focused tests:

```sh
python3 -m pytest tests/test_ios_perception_contract_v2.py tests/test_perception.py -q
python3 -m py_compile backend/perception/ios_contract_v2.py backend/perception/service.py backend/perception/routes.py
git diff --check
```
