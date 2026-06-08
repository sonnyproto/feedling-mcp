"""Capability catalog — the single source of truth for Extended Perception.

Every perceptual capability is declared here ONCE. This drives all the generic
machinery:
  - the report endpoint (which input fields are accepted, which permission gates
    them, how raw values resolve to labels, which state fields they produce)
  - the snapshot endpoint (which fields are cheap context, their freshness TTL)
  - wake triggering (which capabilities are wake sources + their debounce)
  - the transparency UI (label + tier + default-on per capability)

Adding a Tier 2 capability = adding rows here + (if it has query tools) a thin
MCP pass-through. No changes to service/routes logic.

Privacy: capabilities whose `resolver` is set accept RAW values (lat/lon, ssid,
bundle id) and resolve them to coarse labels via the user's perception_config.
The raw value is used transiently and never written to perception_state, so the
agent only ever sees labels.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Capability:
    """One opt-in perceptual ability."""
    key: str                 # permission key == one opt-in toggle
    label: str               # human copy for the transparency UI
    tier: int                # 1 = ships with V2, 2 = follow-up
    wake_source: bool = False
    debounce_sec: float = 0.0
    context_field: bool = False   # appears in cheap wake snapshot
    query_tool: bool = False      # agent pulls on demand
    gated_by: str | None = None   # also requires this capability enabled
    default_on: bool = False


@dataclass(frozen=True)
class Signal:
    """One field the client may report, mapped to its capability + processing."""
    input: str                       # key inside report `signals`
    capability: str                  # permission key gating it
    outputs: tuple[str, ...]         # state field name(s) produced
    resolver: str | None = None      # name in resolve.RESOLVERS, or None (store as-is)
    ttl_sec: float = 600.0           # snapshot freshness; older -> null
    significant: bool = True         # value change can trigger a wake (if wake_source)


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

CAPABILITIES: dict[str, Capability] = {c.key: c for c in [
    # --- always-on (no iOS permission) ---
    Capability("time", "本地时间", 1, context_field=True, default_on=True),
    Capability("device", "电量", 1, context_field=True, default_on=True),
    Capability("broadcast", "屏幕采集状态", 1, context_field=True, default_on=True),

    # --- permissioned ---
    Capability("location", "你大概在哪里（只看地点标签，不看具体地址）", 1,
               wake_source=True, debounce_sec=60.0, context_field=True, query_tool=True),
    Capability("motion", "你在动还是静止", 1,
               wake_source=True, debounce_sec=30.0, context_field=True),
    Capability("calendar", "日历下一场日程", 1, context_field=True, query_tool=True),
    Capability("now_playing", "你在听的音乐", 1, context_field=True, query_tool=True),
    Capability("app", "你在用哪个 app（通过 iOS 快捷指令上报）", 1,
               context_field=True, query_tool=True, default_on=True),
    Capability("photos", "你拍的照片", 2, wake_source=True, query_tool=True),
    Capability("health_sleep", "睡眠", 2, query_tool=True),
    Capability("health_workout", "运动", 2, query_tool=True),
    Capability("health_vitals", "身体趋势", 2, query_tool=True),
]}


# ---------------------------------------------------------------------------
# Signals (report inputs) — keyed by the iOS context_snapshot `key`.
# `data` (a JSON string) parses into the shapes in perception-report-fields.md;
# the resolver picks out the label/state fields and DISCARDS raw/precise fields
# (coordinates, BSSID, placemark address) so the agent only sees coarse state.
# ---------------------------------------------------------------------------

SIGNALS: dict[str, Signal] = {s.input: s for s in [
    # always-on
    Signal("time", "time", ("local_time", "timezone", "locale"),
           resolver="time", ttl_sec=300.0, significant=False),
    Signal("battery", "device", ("battery_level", "charging"),
           resolver="battery", ttl_sec=600.0, significant=False),
    Signal("broadcast", "broadcast", ("broadcast_state", "broadcast_active"),
           resolver="broadcast", ttl_sec=300.0, significant=False),

    # permissioned
    Signal("location_signal", "location", ("place_label", "wifi_label", "country"),
           resolver="location_signal", ttl_sec=900.0),
    Signal("motion_state", "motion", ("motion_state",), ttl_sec=300.0),
    Signal("calendar_next_event", "calendar", ("calendar_next_event",),
           ttl_sec=3600.0, significant=False),
    Signal("playback", "now_playing", ("now_playing",),
           ttl_sec=600.0, significant=False),
    # `app` is reported via the GET /app_open shortcut endpoint (not /report); this
    # entry exists so app_name/app_category appear in the snapshot with a TTL.
    Signal("app", "app", ("app_name", "app_category"),
           ttl_sec=300.0, significant=False),
]}


# Back-compat aliases: canonical capability names also map to the iOS key.
KEY_ALIASES = {
    "location": "location_signal",
    "motion": "motion_state",
    "now_playing": "playback",
    "calendar": "calendar_next_event",
}

# iOS keys that carry only null placeholders (frontmost_app/silent_mode/focus/
# precise_unlock — not obtainable on iOS). Accepted and silently ignored.
IGNORED_KEYS = {"unsupported"}

# Composite report keys whose `data` expands into several signals. (none now —
# battery is its own iOS key.)
COMPOSITE_KEYS: dict[str, list[str]] = {}


# perception_items kinds (collection-style data; see migration 0002) and the
# capability that gates the GENERIC /items endpoint for each kind.
# NOTE: "photo" is intentionally ABSENT from KIND_CAPABILITY — photos must go
# through the dedicated /photo/evaluate flow (which runs _photo_usable + stores
# the encrypted envelope). Allowing kind=photo via /items would let a caller
# inject a hard-blocked photo doc with status=confirmed and bypass the gate.
# (calendar is reported via /report, not /items.)
ITEM_KINDS = ("photo", "workout", "sleep", "vitals")
KIND_CAPABILITY = {
    "workout": "health_workout",
    "sleep": "health_sleep",
    "vitals": "health_vitals",
}

# Burst de-dup backstop. Clustering is primarily done ON DEVICE (iOS collapses a
# 30s burst and uploads only the representative frame); this window is the
# server-side safety net so a client that uploads several still wakes once.
PHOTO_CLUSTER_SEC = 30.0

# scene_hint — the canonical enum shared with the iOS Vision classifier. The
# client MUST emit one of these strings; anything else is treated as "other"
# (non-sensitive) and may reach the agent. Keep this list in lockstep with iOS.
#
# Two-layer policy (user decision): the platform HARD-blocks ONLY the
# objectively-sensitive, low-false-positive scenes (HARD_BLOCK_SCENES) — those
# never reach the agent. Subjective / contextual scenes (private, receipt) DO
# reach the agent as metadata; the agent self-censors per its prompt (skill.md).
SCENE_HINTS = (
    # non-sensitive — may reach the agent
    "landscape", "food", "people", "pet", "activity", "object", "art",
    "text_note", "other",
    # contextual — reach the agent, which self-censors (NOT hard-blocked)
    "private", "receipt",
    # hard-blocked at the gate — never reach the agent
    "document", "id_card", "medical", "screenshot",
)
HARD_BLOCK_SCENES = {"document", "id_card", "medical", "screenshot"}

# Extended on-device metadata the iOS Vision pass may include. The platform gate
# only READS `scene_hint` + `is_screenshot`; everything else is passed through to
# the agent as context for ITS judgment (e.g. is_indoor/has_text_block help the
# agent decide whether a 'private'/'receipt' photo is worth a comment).
PHOTO_METADATA_FIELDS = (
    "has_faces", "face_count", "scene_hint", "scene_confidence",
    "time_of_day", "is_burst", "is_indoor", "has_text_block", "is_screenshot",
)
# "Back after long lock" wake threshold.
UNLOCK_BACK_THRESHOLD_SEC = 1800.0  # 30 min


def signals_for_capability(cap_key: str) -> list[Signal]:
    return [s for s in SIGNALS.values() if s.capability == cap_key]


def context_field_names() -> list[str]:
    """All state field names that belong to context_field capabilities."""
    out: list[str] = []
    for s in SIGNALS.values():
        cap = CAPABILITIES.get(s.capability)
        if cap and cap.context_field:
            out.extend(s.outputs)
    return out
