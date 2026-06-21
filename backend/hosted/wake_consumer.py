"""Hosted proactive wake consumer + tick scheduler (model_api route only)."""

import base64
import copy
import hashlib
import io
import json
import os
import re
import secrets
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Mapping

import httpx
from flask import Blueprint, Response, jsonify, request, g

import db
from core import envelope as core_envelope
from core.store import UserStore

from hosted_runtime import (
    ACTION_RESPONSE_FORMAT as HOSTED_RUNTIME_ACTION_RESPONSE_FORMAT,
    ACTION_METHOD as HOSTED_RUNTIME_ACTION_METHOD,
    BACKGROUND_METHOD as HOSTED_RUNTIME_BACKGROUND_METHOD,
    BACKGROUND_NOT_STARTED_METHOD as HOSTED_RUNTIME_BACKGROUND_NOT_STARTED_METHOD,
    NOOP_METHOD as HOSTED_RUNTIME_NOOP_METHOD,
    PENDING_CONFIRM_METHOD as HOSTED_RUNTIME_PENDING_CONFIRM_METHOD,
    PENDING_REJECT_METHOD as HOSTED_RUNTIME_PENDING_REJECT_METHOD,
    RUNTIME_ENGINE_NATIVE as HOSTED_RUNTIME_ENGINE_NATIVE,
    build_background_execution_messages as build_hosted_runtime_background_execution_messages,
    background_execution_trace as hosted_runtime_background_trace,
    companion_turn_contract_message as hosted_runtime_companion_turn_contract_message,
    coerce_pending_decision as coerce_hosted_runtime_pending_decision,
    coerce_runtime_action as coerce_hosted_runtime_action,
)
from model_api_runtime.prompts import (
    build_foreground_chat_messages as build_model_api_foreground_chat_messages,
    build_memory_capture_messages as build_model_api_memory_capture_messages,
    build_pending_confirmation_messages as build_model_api_pending_confirmation_messages,
    build_web_search_results_message as build_model_api_web_search_results_message,
    web_search_followup_message as model_api_web_search_followup_message,
)
from model_api_runtime.tools import (
    extract_web_search_requests as extract_model_api_web_search_requests,
    run_web_searches as run_model_api_web_searches,
    web_search_trace as model_api_web_search_trace,
)
from model_api_runtime.wake import (
    wake_turn_contract_message as model_api_wake_turn_contract_message,
    build_wake_event_message as build_model_api_wake_event_message,
    hosted_tick_trigger as model_api_hosted_tick_trigger,
    parse_wake_actions as parse_model_api_wake_actions,
)
from context_memory_selection import memory_relevance_details
from content_encryption import build_envelope

from accounts import onboarding as accounts_onboarding
from accounts import registry
from core import store as core_store
from core import enclave as core_enclave
from proactive import gate as proactive_gate
from proactive.adapters_v2 import legacy_job_from_wake_event_v2, wake_event_v2_from_legacy_job
from proactive.agent_protocol_v2 import actions_for_persistence_v2
from proactive.controls_v2 import WakeControlDecisionV2, evaluate_delivery_v2, resolve_settings_v2
from proactive.observability_v2 import DBRuntimeMetricsSinkV2
from proactive.runtime_v2 import RuntimeSpineV2, TurnOutcomeV2, TurnRunnerV2
from proactive.scheduled_wake_v2 import DBScheduledWakeStoreV2, ScheduledWakeServiceV2
from proactive.store_v2 import (
    DBBackgroundJobStoreV2,
    DBProactiveSettingsStoreV2,
    DBTurnLeaseRegistryV2,
    DBTurnStoreV2,
    DBWakeInboxV2,
)
from push import service as push_service
import provider_client
from hosted import config_store as hosted_config_store
from hosted import context as hosted_context
from hosted import turn as hosted_turn
from proactive.tool_loop_v2 import run_tool_loop_v2
from proactive.tool_executor_v2 import ToolExecutorV2, ToolCallV2, combined_runtime_adapters_v2


# Injected by the assembly layer: the Flask app object (threads need an app
# context for the push helpers). app.py sets this at startup.
flask_app = None

HOSTED_WAKE_CONSUMER_ID = "hosted_runtime"
HOSTED_WAKE_CONSUMER_ID_V2 = "hosted_runtime_v2"
HOSTED_WAKE_RUNTIME_V2_FLAG = "hosted_wake_runtime_v2_enabled"
MAX_TOOL_ITERS_V2 = 4
# Thinking/reasoning models share this budget between reasoning and output
# tokens (same note as the chat path) — too small and the visible reply gets
# truncated into an unparseable_wake_reply failure.
HOSTED_WAKE_MAX_TOKENS = 2048


def _hosted_wake_base_eligible(store: UserStore) -> tuple[bool, str]:
    """Static preconditions for consuming ANY hosted wake job: hosted route,
    tested provider config, and a usable plaintext api_key in the process
    cache. The enabled/dnd gate is deliberately NOT here — it is per-job
    (_hosted_wake_job_block) because manual and forced summons bypass it."""
    if accounts_onboarding._load_onboarding_route(store) != "model_api":
        return False, "route_not_model_api"
    config = hosted_config_store._load_model_api_config(store)
    if not config or config.get("test_status") != "ok":
        return False, "model_api_not_ready"
    if not store.last_seen_api_key:
        return False, "api_key_unavailable"
    return True, ""


def _hosted_wake_job_block(store: UserStore, job: dict) -> str:
    """Per-job mechanical gate, mirroring proactive_gate._build_proactive_v2_wake_decision:
    forced wakes bypass the enabled switch, manual wakes (incl. forced —
    the decision builder folds force into manual) bypass dnd. A blocked job
    is terminally skipped, not deferred — a wake describes a moment, and
    running it later when the user re-enables proactive would be wrong."""
    settings = store.load_proactive_settings()
    if not settings.get("enabled", True) and not job.get("forced"):
        return "proactive_disabled"
    if settings.get("dnd", False) and not job.get("manual"):
        return "dnd_enabled"
    return ""


# Concurrency caps for the per-job consumer threads. A capped-out job simply
# stays pending — the reconcile pass in the tick loop retries it — so the cap
# bounds resource use without dropping work.
HOSTED_WAKE_MAX_PER_USER = 2
HOSTED_WAKE_MAX_TOTAL = 16
_hosted_wake_slots_lock = threading.Lock()
_hosted_wake_slots: dict[str, int] = {}  # user_id -> active consumer threads


def _hosted_wake_slot_acquire(user_id: str) -> bool:
    with _hosted_wake_slots_lock:
        if _hosted_wake_slots.get(user_id, 0) >= HOSTED_WAKE_MAX_PER_USER:
            return False
        if sum(_hosted_wake_slots.values()) >= HOSTED_WAKE_MAX_TOTAL:
            return False
        _hosted_wake_slots[user_id] = _hosted_wake_slots.get(user_id, 0) + 1
        return True


def _hosted_wake_slot_release(user_id: str) -> None:
    with _hosted_wake_slots_lock:
        n = _hosted_wake_slots.get(user_id, 0) - 1
        if n > 0:
            _hosted_wake_slots[user_id] = n
        else:
            _hosted_wake_slots.pop(user_id, None)


def _spawn_hosted_wake_thread(store: UserStore, job: dict) -> bool:
    """Start one consumer thread under the concurrency caps. Returns False
    when at capacity — the job stays pending and the reconcile pass retries."""
    if not _hosted_wake_slot_acquire(store.user_id):
        return False
    try:
        threading.Thread(
            target=_run_model_api_wake_job,
            args=(store, store.last_seen_api_key, dict(job)),
            daemon=True,
        ).start()
        return True
    except Exception:
        _hosted_wake_slot_release(store.user_id)
        raise


def _maybe_start_hosted_wake_consumer(store: UserStore, job: dict) -> None:
    """append_proactive_job hook: hosted users get an in-backend consumer
    thread per job; resident users are untouched (their consumer polls).
    Only the base gate runs here — the runner applies the per-job enabled/dnd
    gate (with manual/forced bypasses) after claiming, so blocked jobs get a
    terminal skipped status instead of stranding pending."""
    try:
        eligible, _reason = _hosted_wake_base_eligible(store)
        if not eligible:
            return  # stays pending; the reconcile pass retries when base recovers
        if not _spawn_hosted_wake_thread(store, job):
            print(f"[hosted-wake:{store.user_id}] concurrency cap hit, job stays pending")
    except Exception as e:
        print(f"[hosted-wake:{store.user_id}] consumer start failed: {e}")


# core.store must not import the hosted domain — the consumer is attached as
# an append_proactive_job hook here instead (before any request can append).
core_store.on_proactive_job_appended.append(_maybe_start_hosted_wake_consumer)


def _run_model_api_wake_job(store: UserStore, api_key: str, job: dict) -> None:
    """Thread entry: the push path (jsonify deep inside the Live Activity /
    APNs helpers) needs a Flask app context, which a bare daemon thread
    doesn't have. Always releases its concurrency slot."""
    try:
        with flask_app.app_context():
            _run_model_api_wake_job_inner(store, api_key, job)
    finally:
        _hosted_wake_slot_release(store.user_id)


def _hosted_wake_runtime_v2_enabled(store: UserStore) -> bool:
    try:
        config = hosted_config_store._load_model_api_config(store) or {}
        profile = hosted_config_store._ensure_model_api_runtime_profile(store, config) or {}
        if HOSTED_WAKE_RUNTIME_V2_FLAG in profile:
            return bool(profile.get(HOSTED_WAKE_RUNTIME_V2_FLAG))
        return bool(config.get(HOSTED_WAKE_RUNTIME_V2_FLAG))
    except Exception as e:
        print(f"[hosted-wake:{store.user_id}] v2 flag load failed, using legacy executor: {e}")
        return False


def _run_model_api_wake_job_inner(store: UserStore, api_key: str, job: dict) -> None:
    if _hosted_wake_runtime_v2_enabled(store):
        return _run_model_api_wake_job_inner_v2(store, api_key, job)
    return _run_model_api_wake_job_inner_legacy(store, api_key, job)


def _run_model_api_wake_job_inner_legacy(store: UserStore, api_key: str, job: dict) -> None:
    """Realize one V2 wake job through the user's own hosted model.

    Claim is atomic (only_if_status=pending) so a duplicate consumer or a
    stray resident poller can never double-realize. Every exit path writes a
    terminal job status — the wake dashboard must never show a silently
    half-consumed job."""
    job_id = str(job.get("job_id") or "")
    if not job_id:
        return
    claimed = store.update_proactive_job(
        job_id,
        {"status": "claimed", "consumer_id": HOSTED_WAKE_CONSUMER_ID},
        only_if_status="pending",
    )
    if claimed is None:
        return  # already taken / not pending
    try:
        eligible, reason = _hosted_wake_base_eligible(store)
        if not eligible:
            store.update_proactive_job(job_id, {"status": "skipped", "status_reason": reason})
            return
        block = _hosted_wake_job_block(store, job)
        if block:
            store.update_proactive_job(job_id, {"status": "skipped", "status_reason": block})
            return
        runtime = hosted_config_store._load_runtime_provider_config(store, api_key)
        if isinstance(runtime, tuple):
            _, err = runtime
            store.update_proactive_job(job_id, {
                "status": "failed",
                "status_reason": str(err.get("error") or "runtime_load_failed")[:500],
            })
            return

        wake_settings = store.load_proactive_settings()
        wake_event_msg = build_model_api_wake_event_message(
            job,
            user_directive=str(wake_settings.get("wake_directive") or ""),
        )
        provider_messages, _context_payload, _screen_images = hosted_context._model_api_context_messages(
            store,
            api_key,
            wake_event_msg["content"],
            include_screen_context=False,
        )
        provider_messages.insert(2, model_api_wake_turn_contract_message())
        store.update_proactive_job(job_id, {"status": "realizing"})
        result = provider_client.chat_completion(
            runtime,
            provider_messages,
            max_tokens=HOSTED_WAKE_MAX_TOKENS,
            temperature=0.7,
            timeout=90.0,
            include_reasoning=hosted_turn.MODEL_API_PROVIDER_REASONING_ENABLED,
        )
        actions = parse_model_api_wake_actions(str(result.get("reply") or ""))
        if actions is None:
            # 不把原始回包写进 chat —— 防内部 JSON/推理泄给用户。
            store.update_proactive_job(job_id, {
                "status": "failed", "status_reason": "unparseable_wake_reply",
            })
            return

        executed: list[dict] = []
        sent_message_ids: list[str] = []
        message_attempts = 0
        for action in actions:
            if action["type"] == "send_message":
                message_attempts += 1
                env, env_err = core_envelope._build_shared_envelope_for_store(
                    store, action["text"].encode("utf-8"))
                if env is None:
                    executed.append({"type": "send_message",
                                     "status": f"envelope_failed:{str(env_err)[:120]}"})
                    continue
                row = store.append_chat(
                    "openclaw", "model_api", env,
                    extra={"proactive_job_id": job_id},
                )
                store.notify_chat_waiters()
                delivery_fields = push_service._deliver_ai_message_push_if_background(
                    store,
                    body=action["text"],
                    title="IO",
                    data={"source": "model_api_proactive"},
                    visual_state="reply",
                )
                store.update_chat_message_metadata(row["id"], delivery_fields)
                sent_message_ids.append(row["id"])
                executed.append({"type": "send_message", "status": "posted",
                                 "chat_message_id": row["id"]})
            elif action["type"] == "set_ai_state":
                store.save_proactive_settings({"ai_state": action["state"]})
                executed.append({"type": "set_ai_state", "status": "ok",
                                 "state": action["state"]})
            else:  # sleep
                executed.append({"type": "sleep", "status": "ok"})

        if message_attempts and not sent_message_ids:
            # 模型想说话但一条都没写成（如 enclave 故障导致信封创建全部
            # 失败）——这是投递失败，不是 sleep。标 failed 让 dashboard 和
            # 后续重试看到真相，不能让"成功 sleep"吞掉主动消息。
            store.update_proactive_job(job_id, {
                "status": "failed",
                "status_reason": "message_envelope_failed",
                "agent_action": "send_message_failed",
                "agent_actions": executed[:10],
            })
            return
        if sent_message_ids:
            wake_result = "message_sent"
        elif job.get("manual"):
            # 用户主动召唤却没有任何可见回应 —— V2 评审词表里的
            # ignored_manual，单独标出供 dashboard 复盘（契约要求 manual
            # 至少给最小回应，模型违约时这里如实记录）。
            wake_result = "ignored_manual"
        elif any(a.get("type") == "set_ai_state" for a in executed):
            wake_result = "state_updated"
        else:
            wake_result = "sleep"
        final_patch = {
            "status": "completed",
            "wake_result": wake_result,
            "agent_action": wake_result,
            "agent_actions": executed[:10],
        }
        if sent_message_ids:
            final_patch["chat_message_id"] = sent_message_ids[0]
            final_patch["posted_at"] = datetime.now().isoformat()
        store.update_proactive_job(job_id, final_patch)
        print(f"[hosted-wake:{store.user_id}] job={job_id} result={wake_result}")
    except provider_client.ProviderError as e:
        store.update_proactive_job(job_id, {
            "status": "failed",
            "status_reason": f"provider_chat_failed:{str(e)[:200]}",
        })
    except Exception as e:
        store.update_proactive_job(job_id, {
            "status": "failed",
            "status_reason": f"wake_runner_error:{type(e).__name__}:{str(e)[:200]}",
        })


def _hosted_v2_settings(store: UserStore):
    try:
        return DBProactiveSettingsStoreV2().load(store.user_id)
    except Exception:
        return resolve_settings_v2(store.load_proactive_settings())


def _hosted_v2_recent_chat_provider(store: UserStore, api_key: str | None):
    def _provider(_user_id: str):
        try:
            hist, _hist_err = core_enclave._enclave_get_json_for_gate(
                "/v1/chat/history",
                api_key,
                {"limit": "20", "context_mode": "model_api", "context_trace": "0"},
            )
        except Exception:
            return ()
        if not isinstance(hist, dict) or not isinstance(hist.get("messages"), list):
            return ()
        out: list[dict[str, Any]] = []
        for item in hist.get("messages", [])[-20:]:
            if not isinstance(item, dict):
                continue
            text = str(
                item.get("content")
                or item.get("text")
                or item.get("message")
                or ""
            ).strip()
            if not text:
                continue
            out.append({
                "role": str(item.get("role") or item.get("sender") or "")[:40],
                "text": text[:4000],
                "created_at": str(item.get("created_at") or item.get("ts") or "")[:80],
            })
        return tuple(out)

    return _provider


def _hosted_wake_v2_provider_messages(agent_context: Mapping[str, Any]) -> list[dict[str, str]]:
    payload = json.dumps(dict(agent_context), ensure_ascii=False, sort_keys=True, default=str)
    return [
        {
            "role": "system",
            "content": (
                "You are running Feedling's Proactive/Perception Runtime V2 for the user's hosted IO companion. "
                "Use the provided V2 turn context, tools catalog, switches, local_time/timezone, recent_chat, "
                "change_digest, and background_payloads to decide what the companion should do now. "
                "Return JSON only with this shape: {\"messages\":[\"...\"],\"actions\":[...],\"needs_background\":false,"
                "\"background_request\":{}}. Prefer visible chat text in top-level messages. "
                "If you emit send_message actions, this runtime normalizes their text into messages and dedupes same text, "
                "so do not repeat the same visible text in both places. "
                "If manual=true, include at least one visible message. If nothing should be said, return a sleep action. "
                "To gather information before acting, return {\"tool_calls\":[{\"name\":\"<tool>\",\"args\":{...}}]} "
                "using the tools listed in context; you will get their results and may call more (a few rounds max). "
                "When done gathering, finish with messages/actions and NO tool_calls."
            ),
        },
        {
            "role": "user",
            "content": "V2 turn context JSON:\n" + payload,
        },
    ]


def _hosted_wake_v2_run_agent(runtime, store, api_key):
    def _run(agent_context: Mapping[str, Any]) -> str:
        base_messages = _hosted_wake_v2_provider_messages(agent_context)
        executor = ToolExecutorV2(adapters=combined_runtime_adapters_v2(api_key, store))

        def call_model(messages):
            result = provider_client.chat_completion(
                runtime, messages, max_tokens=HOSTED_WAKE_MAX_TOKENS, temperature=0.7,
                timeout=90.0, include_reasoning=hosted_turn.MODEL_API_PROVIDER_REASONING_ENABLED)
            return str(result.get("reply") or "")

        def call_tool(name, args):
            return executor.execute(
                ToolCallV2(name=name, args=args, user_id=store.user_id)).as_dict()

        return run_tool_loop_v2(call_model, call_tool, base_messages, max_iters=MAX_TOOL_ITERS_V2)

    return _run


def _hosted_wake_v2_runtime(store: UserStore, runtime, api_key: str | None) -> tuple[RuntimeSpineV2, TurnRunnerV2]:
    settings_store = DBProactiveSettingsStoreV2()
    metrics_sink = DBRuntimeMetricsSinkV2()
    spine = RuntimeSpineV2(
        inbox=DBWakeInboxV2(),
        settings_resolver=settings_store.load,
        metrics_sink=metrics_sink,
        # Hosted wake execution is job-driven: each compat job is already the
        # scheduling/ingress unit and is guarded by the foreground turn lease.
        # Keep the merge delay at zero here so hosted worker threads do not
        # sit on claimed legacy jobs; resident/inbox runtimes can use a
        # non-zero merge window when they own the queue end-to-end.
        merge_window_sec=0.0,
    )
    runner = TurnRunnerV2(
        spine,
        run_agent=_hosted_wake_v2_run_agent(runtime, store, api_key),
        recent_chat_provider=_hosted_v2_recent_chat_provider(store, api_key),
        turn_store=DBTurnStoreV2(),
        background_jobs=DBBackgroundJobStoreV2(),
        scheduled_wakes=ScheduledWakeServiceV2(
            DBScheduledWakeStoreV2(),
            owner_id=HOSTED_WAKE_CONSUMER_ID_V2,
        ),
        turn_leases=DBTurnLeaseRegistryV2(),
        metrics_sink=metrics_sink,
        owner_id=HOSTED_WAKE_CONSUMER_ID_V2,
    )
    return spine, runner


def _hosted_wake_v2_deliver_messages(
    store: UserStore,
    *,
    job_id: str,
    result,
) -> tuple[list[str], list[dict[str, Any]]]:
    outcome = result.outcome
    if outcome is None:
        return [], []
    settings = _hosted_v2_settings(store)
    context = result.context
    source = str(getattr(context, "trigger", "") or "")
    if source == "user_message":
        source = "user_message"
    delivery = evaluate_delivery_v2(
        settings,
        source=source,
        manual=bool(getattr(context, "manual", False)),
    )
    sent_message_ids: list[str] = []
    attempts: list[dict[str, Any]] = []
    for text in tuple(getattr(outcome, "messages", ()) or ()):
        body = str(text or "").strip()
        if not body:
            continue
        env, env_err = core_envelope._build_shared_envelope_for_store(store, body.encode("utf-8"))
        if env is None:
            attempts.append({"type": "message", "status": f"envelope_failed:{str(env_err)[:120]}"})
            continue
        row = store.append_chat(
            "openclaw",
            "model_api",
            env,
            extra={"proactive_job_id": job_id, "model_api_kind": "proactive_v2"},
        )
        store.notify_chat_waiters()
        if delivery.allow_visible_delivery:
            delivery_fields = push_service._deliver_ai_message_push_if_background(
                store,
                body=body,
                title="IO",
                data={"source": "model_api_proactive_v2"},
                visual_state="reply",
            )
        else:
            delivery_fields = {
                "push_decision": "suppressed",
                "push_reason": delivery.reason,
                "alert_status": "suppressed",
                "alert_reason": delivery.reason,
            }
        store.update_chat_message_metadata(row["id"], delivery_fields)
        sent_message_ids.append(row["id"])
        attempts.append({
            "type": "message",
            "status": "posted",
            "chat_message_id": row["id"],
            "visible_delivery": bool(delivery.allow_visible_delivery),
            "delivery_reason": delivery.reason,
        })
    return sent_message_ids, attempts


def _hosted_wake_v2_final_patch(result, sent_message_ids: list[str], delivery_attempts: list[dict[str, Any]]) -> dict:
    outcome = result.outcome or TurnOutcomeV2()
    actions = [dict(action) for action in actions_for_persistence_v2(outcome)]
    scheduled_results = [dict(item) for item in (getattr(result, "scheduled_action_results", ()) or ())]
    if sent_message_ids:
        wake_result = "message_sent"
    elif result.status == "ignored_manual":
        wake_result = "ignored_manual"
    elif result.status == "background_queued":
        wake_result = "background_queued"
    elif any(action.get("type") == "schedule_wake" for action in actions):
        wake_result = "scheduled"
    else:
        wake_result = "sleep"
    agent_actions = actions[:10]
    agent_actions.extend(scheduled_results[: max(0, 10 - len(agent_actions))])
    agent_actions.extend(delivery_attempts[: max(0, 10 - len(agent_actions))])
    patch: dict[str, Any] = {
        "status": "completed",
        "wake_result": wake_result,
        "agent_action": wake_result,
        "agent_actions": agent_actions[:10],
    }
    if scheduled_results:
        patch["scheduled_action_results"] = scheduled_results[:10]
    if sent_message_ids:
        patch["chat_message_id"] = sent_message_ids[0]
        patch["posted_at"] = datetime.now().isoformat()
    return patch


def _run_model_api_wake_job_inner_v2(store: UserStore, api_key: str, job: dict) -> None:
    job_id = str(job.get("job_id") or "")
    if not job_id:
        return
    claimed = store.update_proactive_job(
        job_id,
        {"status": "claimed", "consumer_id": HOSTED_WAKE_CONSUMER_ID_V2},
        only_if_status="pending",
    )
    if claimed is None:
        return
    try:
        eligible, reason = _hosted_wake_base_eligible(store)
        if not eligible:
            store.update_proactive_job(job_id, {"status": "skipped", "status_reason": reason})
            return
        runtime = hosted_config_store._load_runtime_provider_config(store, api_key)
        if isinstance(runtime, tuple):
            _, err = runtime
            store.update_proactive_job(job_id, {
                "status": "failed",
                "status_reason": str(err.get("error") or "runtime_load_failed")[:500],
            })
            return

        settings = _hosted_v2_settings(store)
        job_for_adapter = dict(job)
        job_for_adapter.setdefault("timezone", settings.timezone)
        event = wake_event_v2_from_legacy_job(store.user_id, job_for_adapter)
        spine, runner = _hosted_wake_v2_runtime(store, runtime, api_key)
        decision = spine.submit(event)
        if not decision.accepted:
            store.update_proactive_job(job_id, {
                "status": "skipped",
                "status_reason": decision.reason,
                "wake_result": decision.reason,
                "agent_action": decision.reason,
            })
            return
        store.update_proactive_job(job_id, {"status": "realizing"})
        result = runner.run_ready_turn(store.user_id, owner_id=HOSTED_WAKE_CONSUMER_ID_V2)
        if result.status == "busy":
            store.update_proactive_job(job_id, {"status": "pending", "status_reason": "v2_turn_busy"}, only_if_status="realizing")
            return
        if result.status == "idle":
            store.update_proactive_job(job_id, {"status": "pending", "status_reason": "v2_inbox_waiting"}, only_if_status="realizing")
            return
        if result.status == "turn_record_unavailable":
            store.update_proactive_job(job_id, {"status": "failed", "status_reason": "v2_turn_record_unavailable"})
            return
        if result.outcome is None:
            store.update_proactive_job(job_id, {"status": "failed", "status_reason": f"v2_turn_{result.status}"})
            return

        sent_message_ids, delivery_attempts = _hosted_wake_v2_deliver_messages(store, job_id=job_id, result=result)
        if result.outcome.messages and not sent_message_ids:
            store.update_proactive_job(job_id, {
                "status": "failed",
                "status_reason": "message_envelope_failed",
                "agent_action": "send_message_failed",
                "agent_actions": delivery_attempts[:10],
            })
            return
        store.update_proactive_job(job_id, _hosted_wake_v2_final_patch(result, sent_message_ids, delivery_attempts))
        print(f"[hosted-wake-v2:{store.user_id}] job={job_id} result={result.status}")
    except provider_client.ProviderError as e:
        store.update_proactive_job(job_id, {
            "status": "failed",
            "status_reason": f"provider_chat_failed:{str(e)[:200]}",
        })
    except Exception as e:
        store.update_proactive_job(job_id, {
            "status": "failed",
            "status_reason": f"wake_runner_v2_error:{type(e).__name__}:{str(e)[:200]}",
        })


HOSTED_TICK_INTERVAL_SEC = float(os.environ.get("FEEDLING_HOSTED_TICK_INTERVAL_SEC", "1800"))
HOSTED_TICK_LOOP_SEC = 60.0
# Reconcile leases: a claimed/realizing hosted job older than this means the
# process died mid-run (provider timeout is 90s, so 10 min is generous).
HOSTED_WAKE_LEASE_SEC = 600.0
# Pending jobs without an expires_at (perception wakes) go stale after this —
# the context_hint describes a moment, not a standing request.
HOSTED_WAKE_PENDING_MAX_AGE_SEC = 3600.0


def _job_age_ref_epoch(job: dict) -> float:
    """Best timestamp for staleness checks: updated_at (ISO) else ts."""
    raw = str(job.get("updated_at") or "")
    if raw:
        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            pass
    try:
        return float(job.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _reconcile_hosted_jobs(store: UserStore, now: float) -> None:
    """Self-heal the hosted consumer across restarts/crashes. The append hook
    is the only normal consumer, so anything it missed (process died between
    append and thread completion, or the concurrency cap deferred a spawn)
    would otherwise strand forever:
      - pending: expired -> skipped; fresh -> re-spawn a consumer (the atomic
        claim makes a duplicate spawn harmless).
      - claimed/realizing owned by hosted_runtime past the lease -> failed.
        NOT re-run: the crash may have landed after the chat write, so a
        re-run could double-send.
    Resident jobs are untouched: this runs only for hosted-eligible users and
    skips claims owned by other consumer_ids."""
    for job in store.list_proactive_jobs(limit=50):
        job_id = str(job.get("job_id") or "")
        if not job_id:
            continue
        status = str(job.get("status") or "")
        if status == "pending":
            expired = False
            raw_exp = str(job.get("expires_at") or "")
            if raw_exp:
                try:
                    expired = datetime.fromisoformat(raw_exp).timestamp() <= now
                except ValueError:
                    pass
            elif now - _job_age_ref_epoch(job) > HOSTED_WAKE_PENDING_MAX_AGE_SEC:
                expired = True
            if expired:
                store.update_proactive_job(job_id, {
                    "status": "skipped", "status_reason": "expired_unconsumed",
                }, only_if_status="pending")
            else:
                _spawn_hosted_wake_thread(store, job)
        elif status in ("claimed", "realizing"):
            if str(job.get("consumer_id") or "") != HOSTED_WAKE_CONSUMER_ID:
                continue
            if now - _job_age_ref_epoch(job) > HOSTED_WAKE_LEASE_SEC:
                store.update_proactive_job(job_id, {
                    "status": "failed",
                    "status_reason": "stale_claim_reaped",
                }, only_if_status=status)
                print(f"[hosted-wake:{store.user_id}] reaped stale {status} job={job_id}")


def _scheduled_event_compat_job(event) -> dict[str, Any]:
    return legacy_job_from_wake_event_v2(event)


def _run_hosted_scheduled_wake_due_once(store: UserStore, now: float) -> int:
    """Fire due V2 timers for this key-held hosted user.

    The hosted V2 flag remains the operational cutover guard. When the flag is
    off, pending timers stay pending instead of being handed to the legacy wake
    executor.
    """
    if not _hosted_wake_runtime_v2_enabled(store):
        return 0
    settings = _hosted_v2_settings(store)
    service = ScheduledWakeServiceV2(
        DBScheduledWakeStoreV2(),
        owner_id=HOSTED_WAKE_CONSUMER_ID_V2,
    )

    def _submit(event):
        job = _scheduled_event_compat_job(event)
        store.append_proactive_job(job)
        return WakeControlDecisionV2(True, "queued_as_compat_job", settings)

    results = service.fire_due_timers(
        store.user_id,
        settings=settings,
        now=now,
        submit_wake=_submit,
        owner_id=HOSTED_WAKE_CONSUMER_ID_V2,
    )
    return len(results)


def _hosted_keyholder_user_ids() -> list[str]:
    """Users whose plaintext key THIS worker currently holds (set on the store by
    auth / WS). Under -w N each worker runs the tick only for its own key-held
    users: heartbeat creation and the model call both need the key, so they must
    run where the key lives. A user with no key on any worker simply waits — the
    same as the single-worker era, where the key stayed empty until the user's
    first request after a restart."""
    with core_store._stores_lock:
        return [
            uid for uid, st in core_store._stores.items()
            if getattr(st, "last_seen_api_key", "")
        ]


def _run_hosted_tick_once(now: float | None = None) -> int:
    """One scheduler pass on THIS worker: reconcile + create heartbeat wakes for
    the users whose key this worker holds. Returns the number of jobs enqueued
    (the append hook consumes them locally — the key is right here). The trigger
    name comes from the user's broadcast state so the V2 mechanical suppression
    treats hosted heartbeats exactly like resident ones.

    Runs on every worker (not leader-elected): the per-user heartbeat slot is
    claimed atomically in the DB (try_stamp_hosted_tick) and each job is consumed
    under the job-status CAS, so concurrent ticks across workers neither
    double-create nor double-consume."""
    now = now or time.time()
    user_ids = _hosted_keyholder_user_ids()
    created = 0
    for user_id in user_ids:
        try:
            store = core_store.get_store(user_id)
            eligible, _reason = _hosted_wake_base_eligible(store)
            if not eligible:
                continue
            # Every pass (60s), not just on heartbeat: rescue stranded jobs
            # (restart/crash/concurrency-cap deferrals) before the dedup gate.
            # Runs on the BASE gate so a pending manual summon still gets
            # consumed (with its dnd bypass) while proactive is dnd'd off.
            _reconcile_hosted_jobs(store, now)
            created += _run_hosted_scheduled_wake_due_once(store, now)
            settings = store.load_proactive_settings()
            # 心跳是自动 wake，不享受 manual/forced 豁免：开关关/勿扰时
            # 不创建（也不浪费 stamp），等用户打开后下一轮恢复。
            if not settings.get("enabled", True) or settings.get("dnd", False):
                continue
            # 先记 ts 再判定：判定被 suppress 也算一次 tick，避免对被
            # suppress 的用户每 60s 重复跑判定。Atomic CAS stamp so two workers
            # that both hold this user's key can't both create a heartbeat in
            # the same interval (returns False if another worker just stamped).
            if not db.try_stamp_hosted_tick(
                user_id,
                {"ts": now, "at": datetime.fromtimestamp(now).isoformat()},
                now,
                HOSTED_TICK_INTERVAL_SEC,
            ):
                continue
            # Same effective source as the decision builder (device events
            # win, settings fall back) — see _effective_broadcast_state.
            decision = proactive_gate._build_proactive_v2_wake_decision(
                store,
                {"trigger": model_api_hosted_tick_trigger(
                    proactive_gate._effective_broadcast_state(store, settings))},
                api_key=store.last_seen_api_key or None,
            )
            store.append_gate_decision(decision)
            if decision.get("should_wake_agent"):
                store.append_proactive_job(proactive_gate._proactive_job_from_decision(decision))
                created += 1
        except Exception as e:
            print(f"[hosted-tick:{user_id}] failed: {e}")
    return created


def _hosted_tick_loop() -> None:
    while True:
        time.sleep(HOSTED_TICK_LOOP_SEC)
        try:
            created = _run_hosted_tick_once()
            if created:
                print(f"[hosted-tick] created={created}")
        except Exception as e:
            print(f"[hosted-tick] loop error: {e}")


def start_tick():
    """Start the hosted proactive heartbeat on THIS worker. Under -w N every
    worker runs its own tick, each key-gated to the users it holds the key for
    (see _run_hosted_tick_once). Called by app.py under the
    FEEDLING_HOSTED_TICK_ENABLED gate."""
    threading.Thread(target=_hosted_tick_loop, daemon=True, name="hosted-tick").start()


def try_consume_pending_for_user(user_id: str) -> None:
    """'proactive' wake-bus handler: when another worker creates/updates a
    proactive job (NOTIFY 'proactive'), the worker holding this user's plaintext
    key consumes any pending hosted jobs right away — otherwise the next tick's
    reconcile would, up to HOSTED_TICK_LOOP_SEC later. The job-status CAS dedups
    against the creating worker's own append hook, so an overlap is harmless.

    Cheap no-op for resident users and for workers that don't hold the key: the
    store is looked up in-cache only (never loaded), so a notify for a user this
    worker doesn't serve costs nothing."""
    with core_store._stores_lock:
        store = core_store._stores.get(user_id)
    if store is None or not getattr(store, "last_seen_api_key", ""):
        return
    try:
        eligible, _reason = _hosted_wake_base_eligible(store)
        if not eligible:
            return
        _reconcile_hosted_jobs(store, time.time())
    except Exception as e:
        print(f"[hosted-wake:{user_id}] cross-worker consume failed: {e}")
