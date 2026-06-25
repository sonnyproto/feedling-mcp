"""Proactive HTTP surface: /v1/proactive/*, /v1/device/events, /debug/proactive."""

import threading
import time
from datetime import datetime

from flask import Blueprint, Response, jsonify, request

from accounts import auth
from core import store as core_store
from core import util
from proactive import dashboard, gate, resident_runtime_v2, service
from proactive.observability_v2 import ROUND3_REVIEW_LABELS_V2
from proactive.tool_executor_v2 import (
    ToolBudgetV2, ToolExecutorV2, ToolCallV2, combined_runtime_adapters_v2,
)

bp = Blueprint("proactive", __name__)

RESIDENT_WAKE_LEASE_SEC = 600.0
RESIDENT_RUNTIME_OWNER_ID_V2 = "resident_runtime_v2"
_HOSTED_CONSUMER_IDS = frozenset({"hosted_runtime", "hosted_runtime_v2"})
FOREGROUND_CHAT_TOOL_BUDGET_MODE_V2 = "foreground_chat_fast"


def _tool_budget_from_payload_v2(payload: dict) -> ToolBudgetV2 | None:
    mode = str(payload.get("budget_mode") or payload.get("budget") or "").strip().lower()
    if mode in {FOREGROUND_CHAT_TOOL_BUDGET_MODE_V2, "fast_only", "foreground_fast"}:
        return ToolBudgetV2(slow_inline_limit=0)
    return None


def _proactive_state_doc(settings: dict) -> dict:
    enabled = bool(settings.get("enabled", True))
    dnd = bool(settings.get("dnd", False))
    return {
        "version": settings.get("version", 2),
        "enabled": enabled,
        "dnd": dnd,
        "ambient": enabled,
        "scheduled": bool(settings.get("scheduled", True)),
        "reminders_delivery": not dnd,
        "user_state": settings.get("user_state", "default"),
        "manual_user_state": settings.get("manual_user_state", settings.get("user_state", "default")),
        "ai_state": settings.get("ai_state", "present"),
        "broadcast_state": settings.get("broadcast_state", "unknown"),
        "updated_at": settings.get("updated_at", ""),
    }


@bp.route("/v1/proactive/settings", methods=["GET", "POST"])
def proactive_settings():
    store = auth.require_user()
    if request.method == "GET":
        return jsonify(store.load_proactive_settings())
    payload = request.get_json(silent=True) or {}
    settings = store.save_proactive_settings(payload)
    return jsonify(settings)


@bp.route("/v1/proactive/state", methods=["GET", "POST"])
def proactive_state():
    store = auth.require_user()
    if request.method == "GET":
        return jsonify(_proactive_state_doc(store.load_proactive_settings()))
    payload = request.get_json(silent=True) or {}
    settings = store.save_proactive_settings({
        key: payload.get(key)
        for key in (
            "user_state",
            "manual_user_state",
            "ai_state",
            "broadcast_state",
            "enabled",
            "dnd",
            "ambient",
            "scheduled",
            "reminders_delivery",
        )
        if key in payload
    })
    return jsonify(_proactive_state_doc(settings))


@bp.route("/v1/device/events", methods=["GET", "POST"])
def device_events():
    store = auth.require_user()
    if request.method == "GET":
        try:
            since = float(request.args.get("since", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid since"}), 400
        try:
            limit = int(request.args.get("limit", 100))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid limit"}), 400
        limit = max(1, min(limit, 200))
        return jsonify({"events": store.list_device_events(since_epoch=since, limit=limit)})

    payload = request.get_json(silent=True) or {}
    event = service._make_device_event(
        source=str(payload.get("source") or "ios"),
        event_type=str(payload.get("type") or payload.get("event_type") or "unknown"),
        payload=payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
    )
    store.append_device_event(event)
    try:
        from perception import service as perception_service  # lazy; proactive can run without perception tests importing it
        if perception_service.perception_ingress_runtime_v2_enabled(store):
            event["perception_v2"] = perception_service.ingest_device_event_v2(store.user_id, event)
    except Exception as e:
        event["perception_v2"] = {"error": f"ingest_failed:{type(e).__name__}"}
    return jsonify(event)


@bp.route("/v1/proactive/tick", methods=["POST"])
def proactive_tick():
    """Create a proactive wake job.

    V2 is agent-owned: the server may mechanically suppress disabled/away
    automatic wakes, but it no longer decrypts frames, calls a platform LLM, or
    requires a memory connection.
    """
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    decision = gate._build_proactive_v2_wake_decision(store, payload, api_key=auth._extract_api_key())
    store.append_gate_decision(decision)

    job = None
    if decision.get("should_wake_agent", decision.get("should_reach_out")):
        job = store.append_proactive_job(gate._proactive_job_from_decision(decision))

    return jsonify({
        "decision": decision,
        "job": job,
        "enqueued": job is not None,
    })


def _job_status_patch(payload: dict, *, default_status: str = "") -> dict:
    status = str(payload.get("status") or default_status).strip().lower()
    reason = str(payload.get("reason") or payload.get("status_reason") or "").strip()
    consumer_id = str(payload.get("consumer_id") or "").strip()
    now_iso = datetime.now().isoformat()
    patch: dict = {}
    if status:
        patch["status"] = status[:80]
        if status == "claimed":
            patch["claimed_at"] = now_iso
        elif status == "realizing":
            patch["realizing_at"] = now_iso
        elif status in {"posted", "delivered"}:
            patch["posted_at"] = now_iso
        elif status == "completed":
            patch["completed_at"] = now_iso
        elif status in {"failed", "skipped"}:
            patch["failed_at"] = now_iso
    if reason:
        patch["status_reason"] = reason[:500]
    if consumer_id:
        patch["consumer_id"] = consumer_id[:160]
    if payload.get("chat_message_id"):
        patch["chat_message_id"] = str(payload.get("chat_message_id"))[:160]
    if payload.get("agent_action"):
        patch["agent_action"] = str(payload.get("agent_action"))[:120]
    if payload.get("agent_action_status"):
        patch["agent_action_status"] = str(payload.get("agent_action_status"))[:240]
    if isinstance(payload.get("agent_actions"), list):
        safe_actions = []
        for action in payload.get("agent_actions", [])[:10]:
            if not isinstance(action, dict):
                continue
            safe_action = {
                str(k)[:80]: (v if isinstance(v, (bool, int, float)) or v is None else str(v)[:500])
                for k, v in action.items()
                if str(k)
            }
            safe_actions.append(safe_action)
        patch["agent_actions"] = safe_actions
    if payload.get("wake_result"):
        patch["wake_result"] = str(payload.get("wake_result"))[:120]
    if payload.get("ai_state"):
        ai_state = service._normalize_proactive_state(payload.get("ai_state"), core_store.PROACTIVE_AI_STATES, "")
        if ai_state:
            patch["ai_state"] = ai_state
    if payload.get("broadcast_state"):
        broadcast_state = service._normalize_proactive_state(payload.get("broadcast_state"), core_store.PROACTIVE_BROADCAST_STATES, "")
        if broadcast_state:
            patch["broadcast_state"] = broadcast_state
    if isinstance(payload.get("request_broadcast"), dict):
        req = payload.get("request_broadcast") or {}
        try:
            duration_sec = int(req.get("duration_sec") or 0)
        except (TypeError, ValueError):
            duration_sec = 0
        patch["request_broadcast"] = {
            "reason": str(req.get("reason") or "")[:500],
            "duration_sec": max(0, min(duration_sec, 3600)),
            "copy": str(req.get("copy") or req.get("message") or "")[:500],
        }
    return patch


def _job_age_ref_epoch(job: dict) -> float:
    for key in ("realizing_at", "claimed_at", "ts"):
        raw = job.get(key)
        if raw in (None, ""):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw).timestamp()
            except ValueError:
                pass
    return 0.0


def _find_proactive_job(store, job_id: str) -> dict | None:
    for row in store.list_proactive_jobs(since_epoch=0, limit=0):
        if str(row.get("job_id") or "") == str(job_id):
            return row
    return None


def _reclaim_stale_resident_jobs(store, *, now: float | None = None) -> int:
    now = time.time() if now is None else float(now)
    reclaimed = 0
    for job in store.list_proactive_jobs(limit=100):
        status = str(job.get("status") or "")
        if status not in {"claimed", "realizing"}:
            continue
        consumer_id = str(job.get("consumer_id") or "")
        if consumer_id in _HOSTED_CONSUMER_IDS:
            continue
        age_ref = _job_age_ref_epoch(job)
        if not age_ref or now - age_ref <= RESIDENT_WAKE_LEASE_SEC:
            continue
        job_id = str(job.get("job_id") or "")
        if not job_id:
            continue
        patched = store.update_proactive_job(job_id, {
            "status": "pending",
            "status_reason": "resident_stale_claim_recovered",
            "consumer_id": f"recovered:{consumer_id}"[:160] if consumer_id else "recovered:resident",
            "recovered_at": datetime.fromtimestamp(now).isoformat(),
        }, only_if_status=status)
        if patched is not None:
            reclaimed += 1
    return reclaimed


def _with_resident_runtime_v2(job: dict, runtime_profile: dict) -> dict:
    out = dict(job or {})
    out["runtime_v2"] = dict(runtime_profile or {})
    return out


def _settings_v2_for_store(store):
    try:
        from proactive.store_v2 import DBProactiveSettingsStoreV2

        return DBProactiveSettingsStoreV2().load(store.user_id)
    except Exception:
        return store.load_proactive_settings()


def _resident_wake_control_decision_v2(store, job: dict):
    if str((job or {}).get("source") or service.PROACTIVE_JOB_SOURCE) != service.PROACTIVE_JOB_SOURCE:
        return None
    try:
        from proactive.adapters_v2 import wake_event_v2_from_legacy_job
        from proactive.controls_v2 import evaluate_wake_control_v2

        event = wake_event_v2_from_legacy_job(store.user_id, job)
        return evaluate_wake_control_v2(
            event.source,
            manual=event.manual,
            settings=_settings_v2_for_store(store),
        )
    except Exception:
        return None


def _resident_pollable_pending_jobs(store, *, since: float, limit: int, runtime_profile: dict) -> list[dict]:
    out: list[dict] = []
    read_limit = max(limit, 100)
    for job in store.list_proactive_jobs(since_epoch=since, limit=read_limit):
        if str(job.get("status") or "pending") != "pending":
            continue
        decision = _resident_wake_control_decision_v2(store, job)
        if decision is not None and not decision.accepted:
            job_id = str(job.get("job_id") or "")
            if job_id:
                store.update_proactive_job(
                    job_id,
                    {
                        "status": "skipped",
                        "status_reason": decision.reason,
                        "wake_result": decision.reason,
                        "agent_action": decision.reason,
                        "agent_action_status": "resident_poll_wake_gate_v2",
                    },
                    only_if_status="pending",
                )
            continue
        out.append(_with_resident_runtime_v2(job, runtime_profile))
        if len(out) >= limit:
            break
    return out


@bp.route("/v1/proactive/jobs/<job_id>/claim", methods=["POST"])
def proactive_job_claim(job_id):
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    patch = _job_status_patch(payload, default_status="claimed")
    job = store.update_proactive_job(job_id, patch, only_if_status="pending")
    if job is None:
        current = _find_proactive_job(store, str(job_id))
        return jsonify({"claimed": False, "job": current, "reason": "not_pending_or_missing"})
    return jsonify({"claimed": True, "job": job})


@bp.route("/v1/proactive/jobs/<job_id>/status", methods=["POST"])
def proactive_job_status(job_id):
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    patch = _job_status_patch(payload)
    if not patch:
        return jsonify({"error": "empty_status_patch"}), 400
    incoming_consumer = str(payload.get("consumer_id") or "").strip()
    current = _find_proactive_job(store, str(job_id))
    current_consumer = str((current or {}).get("consumer_id") or "").strip()
    if incoming_consumer and current_consumer and incoming_consumer != current_consumer:
        return jsonify({
            "error": "consumer_mismatch",
            "job": current,
            "expected_consumer_id": current_consumer,
        }), 409
    job = store.update_proactive_job(job_id, patch)
    if job is None:
        return jsonify({"error": "job_not_found"}), 404
    return jsonify({"job": job})


@bp.route("/v1/proactive/scheduled/actions", methods=["POST"])
def proactive_scheduled_actions():
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    actions = payload.get("actions")
    if not isinstance(actions, list):
        return jsonify({"error": "actions_required"}), 400
    from proactive.adapters_v2 import legacy_job_from_wake_event_v2
    from proactive.scheduled_wake_v2 import DBScheduledWakeStoreV2, ScheduledWakeServiceV2
    from proactive.store_v2 import DBProactiveSettingsStoreV2

    settings = DBProactiveSettingsStoreV2().load(store.user_id)
    scheduler = ScheduledWakeServiceV2(DBScheduledWakeStoreV2(), owner_id=RESIDENT_RUNTIME_OWNER_ID_V2)

    def _submit(event):
        store.append_proactive_job(legacy_job_from_wake_event_v2(event))
        return type("_Accepted", (), {"accepted": True, "reason": "queued_as_compat_job"})()

    results = scheduler.apply_turn_actions(
        store.user_id,
        actions,
        settings=settings,
        turn_id=str(payload.get("turn_id") or ""),
        wake_ids=tuple(str(item) for item in (payload.get("wake_ids") or ()) if str(item)),
        origin_refs=tuple(str(item) for item in (payload.get("origin_refs") or ()) if str(item)),
        submit_wake=_submit,
    )
    return jsonify({"results": [result.as_dict() for result in results]})


@bp.route("/v1/proactive/scheduled/fire", methods=["POST"])
def proactive_scheduled_fire():
    store = auth.require_user()
    from proactive.adapters_v2 import legacy_job_from_wake_event_v2
    from proactive.controls_v2 import WakeControlDecisionV2
    from proactive.scheduled_wake_v2 import DBScheduledWakeStoreV2, ScheduledWakeServiceV2
    from proactive.store_v2 import DBProactiveSettingsStoreV2

    settings = DBProactiveSettingsStoreV2().load(store.user_id)
    scheduler = ScheduledWakeServiceV2(DBScheduledWakeStoreV2(), owner_id=RESIDENT_RUNTIME_OWNER_ID_V2)
    queued_jobs: list[dict] = []

    def _submit(event):
        job = store.append_proactive_job(legacy_job_from_wake_event_v2(event))
        queued_jobs.append(job)
        return WakeControlDecisionV2(True, "queued_as_compat_job", settings)

    results = scheduler.fire_due_timers(
        store.user_id,
        settings=settings,
        submit_wake=_submit,
        owner_id=RESIDENT_RUNTIME_OWNER_ID_V2,
    )
    return jsonify({
        "results": [result.as_dict() for result in results],
        "jobs": queued_jobs,
        "queued": len(queued_jobs),
    })


@bp.route("/v1/proactive/decisions", methods=["GET"])
def proactive_decisions():
    store = auth.require_user()
    try:
        since = float(request.args.get("since", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid since"}), 400
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    limit = max(1, min(limit, 200))
    return jsonify({"decisions": store.list_gate_decisions(since_epoch=since, limit=limit)})


@bp.route("/v1/proactive/decisions/<decision_id>/review", methods=["POST"])
def proactive_decision_review(decision_id):
    store = auth.require_user()
    if request.is_json:
        payload = request.get_json(silent=True) or {}
    else:
        payload = request.form.to_dict(flat=True)

    decision_id = str(decision_id or "").strip()
    if not decision_id:
        return jsonify({"error": "decision_id_required"}), 400

    decision = None
    for row in store.list_gate_decisions(since_epoch=0, limit=0):
        if str(row.get("decision_id") or "") == decision_id:
            decision = row
            break
    if decision is None:
        return jsonify({"error": "decision_not_found"}), 404

    label = str(payload.get("label") or "").strip()
    legacy_gate_labels = {
        "correct_true",
        "correct_false",
        "missed_opportunity",
        "spam",
        "weak_connection",
        "repeated",
        "privacy_bad",
        "great_companion_moment",
    }
    allowed_labels = set(ROUND3_REVIEW_LABELS_V2) | legacy_gate_labels
    if label not in allowed_labels:
        return jsonify({"error": "invalid_label", "allowed": sorted(allowed_labels)}), 400

    review = {
        "review_id": util._new_public_id("gr"),
        "decision_id": decision_id,
        "ts": time.time(),
        "created_at": datetime.now().isoformat(),
        "label": label,
        "label_family": "round3" if label in ROUND3_REVIEW_LABELS_V2 else "legacy_gate",
        "notes": str(payload.get("notes") or "")[:500],
        "reviewer": str(payload.get("reviewer") or "human")[:80],
        "expected_should_reach_out": payload.get("expected_should_reach_out"),
        "correct_connection_source_id": str(payload.get("correct_connection_source_id") or "")[:160],
        "decision_should_reach_out": bool(decision.get("should_reach_out")),
        "decision_reason": str(decision.get("reason") or decision.get("abstention_reason") or "")[:240],
        "decision_intent_label": str(decision.get("intent_label") or "")[:120],
        "decision_connection": decision.get("connection") or {},
        "frame_ids": decision.get("frame_ids") or [],
    }
    store.append_gate_review(review)
    accept = request.headers.get("Accept", "")
    if not request.is_json and "text/html" in accept:
        return Response(
            "<html><body><p>Review saved.</p><script>history.back()</script></body></html>",
            mimetype="text/html",
        )
    return jsonify({"review": review})


@bp.route("/v1/proactive/reviews", methods=["GET"])
def proactive_reviews():
    store = auth.require_user()
    try:
        since = float(request.args.get("since", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid since"}), 400
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    limit = max(1, min(limit, 500))
    return jsonify({"reviews": store.list_gate_reviews(since_epoch=since, limit=limit)})


@bp.route("/v1/proactive/debug", methods=["GET"])
def proactive_debug_json():
    store = auth.require_user()
    return jsonify(dashboard._proactive_debug_snapshot(store))


@bp.route("/debug/proactive", methods=["GET"])
def proactive_debug_page():
    store = auth.require_user()
    html_body = dashboard._render_proactive_dashboard(dashboard._proactive_debug_snapshot(store))
    return Response(html_body, mimetype="text/html")


@bp.route("/v1/proactive/jobs/poll", methods=["GET"])
def proactive_jobs_poll():
    store = auth.require_user()
    _reclaim_stale_resident_jobs(store)
    runtime_profile = resident_runtime_v2.resident_runtime_v2_public_profile(store)
    try:
        since = float(request.args.get("since", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid since"}), 400
    try:
        timeout = max(0.0, min(float(request.args.get("timeout", 30)), 60))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid timeout"}), 400
    try:
        limit = int(request.args.get("limit", 20))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    limit = max(1, min(limit, 100))

    pending = _resident_pollable_pending_jobs(store, since=since, limit=limit, runtime_profile=runtime_profile)
    if pending:
        return jsonify({"jobs": pending, "runtime_v2": runtime_profile, "timed_out": False})

    ev = threading.Event()
    with store.proactive_job_waiters_lock:
        store.proactive_job_waiters.append(ev)

    notified = ev.wait(timeout=timeout)

    with store.proactive_job_waiters_lock:
        try:
            store.proactive_job_waiters.remove(ev)
        except ValueError:
            pass

    if notified:
        pending = _resident_pollable_pending_jobs(store, since=since, limit=limit, runtime_profile=runtime_profile)
        return jsonify({"jobs": pending, "runtime_v2": runtime_profile, "timed_out": False})
    return jsonify({"jobs": [], "runtime_v2": runtime_profile, "timed_out": True})


@bp.route("/v1/proactive/tool/execute", methods=["POST"])
def proactive_tool_execute():
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    if not name:
        return jsonify({"error": "tool name required"}), 400
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    executor = ToolExecutorV2(
        adapters=combined_runtime_adapters_v2(store.last_seen_api_key, store),
        budget=_tool_budget_from_payload_v2(payload),
    )
    result = executor.execute(ToolCallV2(name=name, args=args, user_id=store.user_id))
    return jsonify(result.as_dict())
