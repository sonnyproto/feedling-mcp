"""Extended Perception business logic.

All the generic machinery driven by catalog.py:
  - ingest(): sparse, permission-gated report; resolve raw->label; merge into
    per-field state; trigger debounced wakes on significant change.
  - snapshot(): current authorized+fresh fields; unauthorized/stale -> null.
  - permissions / config views and updates.
  - user_state with the Focus override/restore stack.
  - photo two-step flow (evaluate+stage with sensitivity gate, then confirm).
  - generic collection ingest/read for Tier 2 (calendar / health).

No business logic lives in app.py. The only app.py coupling is a lazy import in
_fire_wake() to enqueue a proactive job (the existing wake mechanism).
"""
from __future__ import annotations

import logging
import json
import time
from datetime import datetime

from content_encryption import random_item_id

from . import catalog, resolve, store

log = logging.getLogger("perception.service")


def _now() -> float:
    return time.time()


_FUTURE_TS_TOLERANCE_SEC = 60.0  # allow minor client clock skew


def _coerce_ts(client_ts) -> float:
    """A report's logical time: its client_ts when present, else now. Using the
    record's own time makes batch/offline replay correct — per-field freshness
    and wake debounce are evaluated against when the signal actually happened.

    A FUTURE timestamp (clock skew, or milliseconds sent instead of seconds) is
    clamped to now: otherwise it would keep the snapshot permanently "fresh" and
    make the ordering guard reject every correctly-timestamped report until
    wall-clock catches up, freezing perception state."""
    now = _now()
    try:
        ts = float(client_ts)
    except (TypeError, ValueError):
        return now
    return now if ts > now + _FUTURE_TS_TOLERANCE_SEC else ts


# ---------------------------------------------------------------------------
# Permission gating
# ---------------------------------------------------------------------------

def is_enabled(user_id: str, cap_key: str, perms: dict | None = None) -> bool:
    perms = perms if perms is not None else store.get_permissions(user_id)
    cap = catalog.CAPABILITIES.get(cap_key)
    if not cap:
        return False
    if not bool(perms.get(cap_key, cap.default_on)):
        return False
    if cap.gated_by and not is_enabled(user_id, cap.gated_by, perms):
        return False
    return True


# ---------------------------------------------------------------------------
# Ingest (generic sparse report)
# ---------------------------------------------------------------------------

def _parse_data(data):
    """A context_snapshot item's `data` is a STRING: JSON, or "null"/""/None for
    "no value". Returns the parsed Python value, None for the null cases, or the
    raw string if it isn't valid JSON (lenient)."""
    if data is None:
        return None
    if isinstance(data, str):
        s = data.strip()
        if s == "" or s.lower() == "null":
            return None
        try:
            return json.loads(s)
        except Exception:
            return s
    return data  # already structured (lenient)


def ingest_snapshot(user_id: str, items: list, client_ts=None) -> dict:
    """Ingest a {context_snapshot:[{key,data,message}]} report. `data` is a JSON
    string (or "null"). Composite keys (e.g. device) expand into their sub-signals;
    aliases (e.g. location_signal -> location) are normalized."""
    pairs: list[tuple] = []  # (input_name, value, message)
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        key = str(it.get("key") or "").strip()
        if not key:
            continue
        msg = it.get("message")
        value = _parse_data(it.get("data"))
        if key in catalog.COMPOSITE_KEYS:
            subs = catalog.COMPOSITE_KEYS[key]
            if isinstance(value, dict):
                for sub in subs:
                    if sub in value:
                        pairs.append((sub, value[sub], msg))
            elif value is None:
                for sub in subs:
                    pairs.append((sub, None, msg))
            continue
        pairs.append((key, value, msg))
    return _apply(user_id, pairs, client_ts)


def ingest(user_id: str, signals: dict, client_ts: float | None = None) -> dict:
    """Back-compat / internal: ingest a flat {key: value} map (no messages)."""
    return _apply(user_id, [(k, v, None) for k, v in (signals or {}).items()], client_ts)


def _cell(value, ts: float, msg) -> dict:
    cell = {"v": value, "ts": ts}
    if msg is not None:
        cell["msg"] = msg
    return cell


def _apply(user_id: str, pairs: list, client_ts=None) -> dict:
    now = _coerce_ts(client_ts)
    perms = store.get_permissions(user_id)
    config = store.get_config(user_id)
    prev_state = store.get_state(user_id)
    results: dict[str, str] = {}
    patch: dict[str, dict] = {}          # field -> candidate cell (ts-guarded on write)
    input_fields: dict[str, list] = {}   # input_name -> output fields it proposed
    wake_pending: list[tuple] = []       # (cap_key, debounce, field, old, new)

    for input_name, value, msg in pairs:
        if input_name in catalog.IGNORED_KEYS:
            results[input_name] = "ignored"  # e.g. "unsupported" (all-null placeholder)
            continue
        key = catalog.KEY_ALIASES.get(input_name, input_name)
        sig = catalog.SIGNALS.get(key)
        if sig is None:
            results[input_name] = "unknown_signal"
            continue
        if not is_enabled(user_id, sig.capability, perms):
            results[input_name] = "dropped_unauthorized"
            continue

        # Focus drives the user_state override stack (value None -> clear).
        if sig.capability == "focus":
            _apply_focus(user_id, value, config)
            results[input_name] = "accepted"
            continue

        fields: list[str] = []
        if value is None:
            # data:"null" -> field unavailable now; record null + message. No wake.
            for fname in sig.outputs:
                patch[fname] = _cell(None, now, msg)
                fields.append(fname)
        else:
            # Resolve raw -> label (raw discarded) or store as-is.
            if sig.resolver:
                fn = resolve.RESOLVERS.get(sig.resolver)
                resolved = fn(value, config) if fn else {}
            else:
                resolved = {sig.outputs[0]: value}
            cap = catalog.CAPABILITIES[sig.capability]
            for fname in sig.outputs:
                if fname not in resolved:
                    continue
                new_v = resolved[fname]
                patch[fname] = _cell(new_v, now, msg)
                fields.append(fname)
                old = (prev_state.get(fname) or {}).get("v")
                if cap.wake_source and sig.significant and new_v != old:
                    wake_pending.append((sig.capability, cap.debounce_sec, fname, old, new_v))
        input_fields[input_name] = fields

    # Atomic ts-guarded write under a row lock: a field is persisted only if its
    # new ts >= the currently-stored ts. Doing the compare-and-write atomically
    # (not against the pre-read prev_state) is what makes the "late older record
    # never clobbers a newer value" guarantee hold under concurrent reports.
    written = store.merge_state_guarded(user_id, patch) if patch else set()

    for input_name, fields in input_fields.items():
        results[input_name] = "accepted" if any(f in written for f in fields) else "stale_ignored"

    # device "back after a long lock" wake (only if the field was actually written).
    _maybe_unlock_wake(user_id, patch, written, perms, prev_state, now, wake_pending)

    for (capk, debounce, f, old, new_v) in wake_pending:
        if f in written:
            _maybe_wake(user_id, capk, debounce, f, old, new_v, now)

    return results


def _apply_focus(user_id: str, value, config: dict) -> None:
    """Maintain the {manual, focus_override} stack. An active Focus overrides the
    manual user_state; clearing Focus (none/null) restores the manual value.
    `value` may be a string, a dict ({ios_focus|focus|state:..}), or None."""
    if isinstance(value, dict):
        raw = value.get("ios_focus") or value.get("focus") or value.get("state")
    else:
        raw = value
    raw = (str(raw) if raw is not None else "none").strip().lower()
    doc = store.get_user_state_doc(user_id)
    doc.setdefault("manual", doc.get("manual") or "default")
    if raw in ("", "none"):
        doc["focus_override"] = None
    else:
        doc["focus_override"] = resolve.resolve_focus(value, config).get("user_state")
    store.set_user_state_doc(user_id, doc)


def _maybe_unlock_wake(user_id, patch, written, perms, prev_state, now, wake_candidates) -> None:
    if "last_unlock_ago_sec" not in written:
        return
    cell = patch.get("last_unlock_ago_sec")
    if not cell or cell.get("v") is None:
        return
    if not is_enabled(user_id, "device", perms):
        return
    try:
        new = float(cell["v"])
    except (TypeError, ValueError):
        return
    prev = (prev_state.get("last_unlock_ago_sec") or {}).get("v")
    try:
        was_long = prev is None or float(prev) >= catalog.UNLOCK_BACK_THRESHOLD_SEC
    except (TypeError, ValueError):
        was_long = True
    if new <= 60 and was_long:
        wake_candidates.append(("device", 0.0, "last_unlock_ago_sec", prev, new))


# ---------------------------------------------------------------------------
# Wake triggering (debounced; reuses the existing proactive-job mechanism)
# ---------------------------------------------------------------------------

def _last_wake_ts(user_id: str, cap_key: str) -> float:
    for ev in reversed(store.read_events(user_id, limit=50)):
        if ev.get("cap") == cap_key and ev.get("type") == "wake":
            return float(ev.get("ts") or 0)
    return 0.0


def _maybe_wake(user_id, cap_key, debounce, field, old, new_v, now) -> None:
    if debounce and (now - _last_wake_ts(user_id, cap_key)) < debounce:
        store.append_event(user_id, {
            "cap": cap_key, "type": "debounced", "field": field,
            "old": old, "new": new_v, "ts": now,
        }, now)
        return
    store.append_event(user_id, {
        "cap": cap_key, "type": "wake", "field": field,
        "old": old, "new": new_v, "ts": now,
    }, now)
    _fire_wake(user_id, cap_key, _wake_hint(cap_key, field, old, new_v), now)


def _wake_hint(cap_key: str, field: str, old, new_v) -> str:
    if cap_key == "location":
        return f"她到了一个新地方：place_label = {new_v}（之前 {old or '未知'}）。"
    if cap_key == "wifi":
        return f"她连上了 {new_v}（之前 {old or '未知'}）。"
    if cap_key == "app":
        return f"她切到了 {new_v} 类应用（之前 {old or '未知'}）。"
    if cap_key == "motion":
        return f"她的运动状态变成了 {new_v}（之前 {old or '未知'}）。"
    if cap_key == "device" and field == "last_unlock_ago_sec":
        return "她长时间锁屏后刚刚解锁——拿起手机回来了。"
    if cap_key == "region":
        return f"她到了 {new_v}——一个明确的'今天联系一下'时刻。"
    return f"{cap_key} 发生了变化：{field} = {new_v}。"


def _fire_wake(user_id: str, cap_key: str, hint: str, now: float) -> None:
    """Enqueue a proactive job so the resident agent wakes. Lazy-imports app to
    avoid an import cycle (app registers this module at the bottom of startup)."""
    try:
        import app  # lazy
        s = app.get_store(user_id)
        job = {
            "job_id": app._new_public_id("pj"),
            "ts": now,
            "created_at": datetime.fromtimestamp(now).isoformat(),
            "source": app.PROACTIVE_JOB_SOURCE,
            "status": "pending",
            "intent_label": f"perception_{cap_key}"[:120],
            "context_hint": hint[:2000],
            "connections": [],
            "connection": {},
            "frame_ids": [],
            "device_event_ids": [],
            "current_app": "",
        }
        s.append_proactive_job(job)
    except Exception as e:
        log.error("fire_wake(%s,%s) failed: %s", user_id, cap_key, e)


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def snapshot(user_id: str, now: float | None = None) -> dict:
    now = now or _now()
    perms = store.get_permissions(user_id)
    state = store.get_state(user_id)
    snap: dict = {}
    for sig in catalog.SIGNALS.values():
        cap = catalog.CAPABILITIES.get(sig.capability)
        if not cap or not cap.context_field:
            continue
        enabled = is_enabled(user_id, sig.capability, perms)
        for f in sig.outputs:
            if f == "user_state":
                continue  # owned by the focus/manual stack, set below
            if not enabled:
                snap[f] = None
                continue
            cell = state.get(f)
            if not isinstance(cell, dict):
                snap[f] = None
                continue
            if (now - float(cell.get("ts") or 0)) > sig.ttl_sec:
                snap[f] = None  # stale -> agent treats as "don't infer"
            else:
                snap[f] = cell.get("v")
    # user_state is always present (manual default if nothing set).
    snap["user_state"] = effective_user_state(user_id)
    return snap


# ---------------------------------------------------------------------------
# user_state
# ---------------------------------------------------------------------------

def effective_user_state(user_id: str) -> str:
    doc = store.get_user_state_doc(user_id)
    override = doc.get("focus_override")
    if override:
        return override
    return doc.get("manual") or "default"


def set_manual_user_state(user_id: str, value: str) -> str:
    doc = store.get_user_state_doc(user_id)
    doc["manual"] = str(value or "default")
    store.set_user_state_doc(user_id, doc)
    return effective_user_state(user_id)


# ---------------------------------------------------------------------------
# Permissions & config
# ---------------------------------------------------------------------------

def permissions_view(user_id: str) -> list[dict]:
    perms = store.get_permissions(user_id)
    return [{
        "key": cap.key,
        "label": cap.label,
        "tier": cap.tier,
        "enabled": is_enabled(user_id, cap.key, perms),
        "wake_source": cap.wake_source,
    } for cap in catalog.CAPABILITIES.values()]


def set_permissions(user_id: str, patch: dict) -> list[dict]:
    clean = {k: bool(v) for k, v in (patch or {}).items() if k in catalog.CAPABILITIES}
    if clean:
        store.merge_permissions(user_id, clean)
        # Disabling a capability must immediately remove its fields from state.
        for k, v in clean.items():
            if not v:
                fields: list[str] = []
                for sig in catalog.signals_for_capability(k):
                    fields.extend(sig.outputs)
                if fields:
                    store.clear_state_fields(user_id, fields)
    return permissions_view(user_id)


def config_view(user_id: str) -> dict:
    return store.get_config(user_id)


def set_config(user_id: str, patch: dict) -> dict:
    return store.merge_config(user_id, patch or {})


# ---------------------------------------------------------------------------
# Photos (two-step, sensitivity-gated)
# ---------------------------------------------------------------------------

def _truthy(v) -> bool:
    """Coerce a bool that may have arrived as a string ("默认字符串"). "false"/
    "0"/"" -> False."""
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v)


def _photo_usable(meta: dict) -> tuple[bool, bool, str]:
    """Two-layer sensitivity gate — metadata only, no pixel decryption. The
    platform HARD-blocks only objectively-sensitive scenes; contextual ones
    (private/receipt) pass and the agent self-censors. Returns
    (usable, sensitive, reason)."""
    scene = str(meta.get("scene_hint") or "").lower()
    if scene in catalog.HARD_BLOCK_SCENES:
        return False, True, f"hard_block:{scene}"
    if _truthy(meta.get("is_screenshot")):
        return False, False, "screenshot"
    return True, False, ""


def photo_evaluate(user_id: str, metadata: dict,
                   content_envelope: dict | None = None,
                   exif_gps: dict | None = None) -> tuple[dict, int]:
    """Single-step photo ingest: evaluate metadata AND (if usable) store the
    encrypted image in one call.

    - Hard-blocked photos (id_card/medical/document/screenshot) are REJECTED and
      their ciphertext — even if uploaded — is discarded: never stored, never
      reaches the agent.
    - Usable photos: the ciphertext goes into the screen-frame envelope channel
      (reuses the enclave's existing frame-decrypt path); the backend never sees
      plaintext. frame_id == photo_id == content_envelope.id.
    """
    if not is_enabled(user_id, "photos"):
        return {"error": "unauthorized", "capability": "photos"}, 403
    now = _now()
    metadata = metadata or {}
    config = store.get_config(user_id)
    place_label = None
    if exif_gps:
        place_label = resolve.resolve_geofence(exif_gps, config).get("place_label")
    usable, sensitive, reason = _photo_usable(metadata)
    photo_id = str((content_envelope or {}).get("id") or random_item_id())
    meta_out = {k: metadata.get(k) for k in catalog.PHOTO_METADATA_FIELDS}
    meta_out["place_label"] = place_label

    if not usable:
        # Hard-blocked: do NOT store anything; any uploaded ciphertext is dropped.
        return {"photo_id": photo_id, "metadata": meta_out, "usable": False,
                "sensitive": sensitive, "reason": reason, "status": "rejected"}, 200

    if not content_envelope:
        return {"error": "content_envelope_required"}, 400

    # Store ciphertext in the frame channel + metadata as a confirmed item.
    store.put_photo_envelope(user_id, photo_id, now, content_envelope)
    doc = {"photo_id": photo_id, "metadata": meta_out, "status": "confirmed",
           "usable": True, "sensitive": False, "frame_id": photo_id}
    store.item_upsert(user_id, "photo", photo_id, now, doc, expires_at=None)

    # Burst de-dup backstop: only wake once per cluster window.
    if (now - _last_wake_ts(user_id, "photos")) >= catalog.PHOTO_CLUSTER_SEC:
        store.append_event(user_id, {"cap": "photos", "type": "wake",
                                     "item": photo_id, "ts": now}, now)
        _fire_wake(user_id, "photos",
                   "她拍了一张可能值得一提的照片（先看元数据，需要再 pull 内容）。", now)
    return {"photo_id": photo_id, "metadata": meta_out, "usable": True,
            "sensitive": False, "status": "stored"}, 200


def photos_recent(user_id: str, limit: int = 20) -> tuple[dict, int]:
    if not is_enabled(user_id, "photos"):
        return {"error": "unauthorized"}, 403
    now = _now()
    items = [i for i in store.item_list(user_id, "photo", limit=limit, now=now)
             if i.get("status") == "confirmed"]
    out = [{"photo_id": i.get("photo_id"), "metadata": i.get("metadata")} for i in items]
    return {"photos": out}, 200


def photo_content(user_id: str, photo_id: str) -> tuple[dict, int]:
    """Permission + status gate for one confirmed photo. Returns metadata and the
    frame_id; the caller decrypts pixels via the enclave's existing
    /v1/screen/frames/<frame_id>/decrypt path. The backend never holds plaintext
    pixels — only the enclave decrypts."""
    if not is_enabled(user_id, "photos"):
        return {"error": "unauthorized"}, 403
    now = _now()
    doc = store.item_get(user_id, "photo", photo_id, now=now)
    if not doc or doc.get("status") != "confirmed":
        return {"error": "not_found"}, 404
    return {
        "photo_id": photo_id,
        "frame_id": doc.get("frame_id") or photo_id,
        "metadata": doc.get("metadata"),
        "decrypt_path": f"/v1/screen/frames/{doc.get('frame_id') or photo_id}/decrypt",
    }, 200


# ---------------------------------------------------------------------------
# Tier 2 collections (calendar / health) — generic
# ---------------------------------------------------------------------------

def items_ingest(user_id: str, kind: str, items: list[dict]) -> tuple[dict, int]:
    cap = catalog.KIND_CAPABILITY.get(kind)
    if not cap:
        return {"error": "unknown_kind"}, 400
    if not is_enabled(user_id, cap):
        return {"error": "unauthorized", "capability": cap}, 403
    now = _now()
    wrote = 0
    for it in (items or []):
        iid = str(it.get("item_id") or random_item_id())
        ts = float(it.get("ts") or now)
        store.item_upsert(user_id, kind, iid, ts, it.get("doc") or {}, it.get("expires_at"))
        wrote += 1
    return {"written": wrote}, 200


def items_recent(user_id: str, kind: str, limit: int = 20) -> tuple[dict, int]:
    cap = catalog.KIND_CAPABILITY.get(kind)
    if not cap:
        return {"error": "unknown_kind"}, 400
    if not is_enabled(user_id, cap):
        return {"error": "unauthorized", "capability": cap}, 403
    now = _now()
    return {"items": store.item_list(user_id, kind, limit=limit, now=now)}, 200


# ---------------------------------------------------------------------------
# App usage (iOS Shortcut GET endpoint) — "what app at what time"
# ---------------------------------------------------------------------------

def app_open(user_id: str, app: str, category: str | None = None,
             client_ts=None) -> tuple[dict, int]:
    """Record one app-open event (fired by an iOS Shortcut when the user opens an
    app). Updates current app in the snapshot AND appends to the usage time series
    for later stats. Gated by the `app` capability (default-on; user can turn it
    off in the transparency panel)."""
    if not is_enabled(user_id, "app"):
        return {"error": "unauthorized", "capability": "app"}, 403
    app = (app or "").strip()
    if not app:
        return {"error": "app_required"}, 400
    now = _coerce_ts(client_ts)
    category = (category or "").strip() or None
    # current app -> snapshot (ts-guarded)
    store.merge_state_guarded(user_id, {
        "app_name": _cell(app, now, None),
        "app_category": _cell(category, now, None),
    })
    # append to the usage time series
    store.append_app_open(user_id, {"app": app, "category": category, "ts": now}, now)
    return {"status": "ok", "app": app, "category": category, "ts": now}, 200


def app_usage(user_id: str, limit: int = 100, since_epoch: float = 0.0) -> tuple[dict, int]:
    """Read the app-usage time series (agent/stats side)."""
    if not is_enabled(user_id, "app"):
        return {"error": "unauthorized", "capability": "app"}, 403
    return {"events": store.read_app_opens(user_id, limit=limit, since_epoch=since_epoch)}, 200
