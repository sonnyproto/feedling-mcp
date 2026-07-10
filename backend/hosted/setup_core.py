"""Framework-neutral hosted-setup operations (ASGI-migration plan §5.3).

A pure relocation of the Flask ``hosted.setup_routes`` route bodies so both the
Flask adapter (``hosted.setup_routes``) and the native FastAPI router
(``hosted.setup_routes_asgi``) share one implementation and return
byte-identical responses.

E2E / enclave boundary (unchanged): ``/v1/model_api/key_envelope`` returns the
caller's OWN ``api_key_envelope`` ciphertext — the server never decrypts it; only
the enclave can. ``model_api_setup`` seals a freshly-supplied provider key into a
shared envelope via ``core.envelope._build_shared_envelope_for_store`` and, when
reusing a saved key, decrypts the existing envelope ONLY through the enclave
(``core.enclave._decrypt_envelope_via_enclave``). These functions take
already-parsed params + the store and the caller's credential as explicit
arguments — they never read ``flask.request`` — so no new server-side plaintext
is ever introduced here. Module-level references (``provider_client``,
``core_enclave`` …) are preserved so tests can monkeypatch the attributes.
"""

from __future__ import annotations

import threading
import uuid

import db
from core import enclave as core_enclave
from core import envelope as core_envelope
from core import util as core_util
from core.store import UserStore
from accounts import onboarding as accounts_onboarding
from memory import service as memory_service
import provider_client
from hosted import agent_runtime_cutover
from hosted import config_store as hosted_config_store
from hosted import turn as hosted_turn


_REASONING_EFFORT_OFF = {"off", "none", "no", "false", "0", "disabled"}
_REASONING_EFFORT_LEVELS = {"low", "medium", "high"}


def _normalize_reasoning_effort(value) -> str | None:
    """Canonical per-user reasoning switch for hosted model_api config.

    ``None`` means the field was absent/blank and should not be persisted, so the
    gateway follows its global default. ``off`` is persisted when explicitly set,
    which gives operations a visible per-user opt-out.
    """
    if value is None:
        return None
    raw = str(value).strip().lower()
    if not raw:
        return None
    if raw in _REASONING_EFFORT_OFF:
        return "off"
    if raw in _REASONING_EFFORT_LEVELS:
        return raw
    if raw.isdigit() and int(raw) > 0:
        return str(int(raw))
    raise ValueError("reasoning_effort must be off, low, medium, high, or a positive integer")


def model_api_setup(store, payload: dict, *, caller_api_key: str | None) -> tuple[dict, int]:
    provider = str(payload.get("provider") or "")
    model = str(payload.get("model") or "")
    base_url = str(payload.get("base_url") or "")
    raw_key = str(payload.get("api_key") or "").strip()
    try:
        reasoning_effort = _normalize_reasoning_effort(payload.get("reasoning_effort"))
    except ValueError as e:
        return {"error": "invalid_reasoning_effort", "detail": str(e)}, 400
    try:
        provider, model, base_url = provider_client.validate_config(provider, model, base_url)
    except provider_client.ProviderError as e:
        return {"error": str(e)}, 400

    existing = hosted_config_store._load_model_api_config(store) or {}
    existing_envelope = existing.get("api_key_envelope")
    if raw_key:
        provider_key = raw_key
        envelope, err = core_envelope._build_shared_envelope_for_store(
            store,
            raw_key.encode("utf-8"),
            item_id=f"model_api_key_{uuid.uuid4().hex}",
        )
        if envelope is None:
            return {
                "error": "cannot_encrypt_provider_key",
                "detail": err,
                "required": (
                    "The user must have a content public key and the enclave "
                    "attestation endpoint must be reachable before saving a provider key."
                ),
            }, 409
        api_key_hint = provider_client.mask_api_key(raw_key)
    else:
        if not isinstance(existing_envelope, dict):
            return {"error": "api_key required"}, 400
        try:
            provider_key = core_enclave._decrypt_envelope_via_enclave(
                existing_envelope,
                caller_api_key,
                purpose="model_api_provider_key",
            ).decode("utf-8")
        except Exception as e:
            return {
                "error": "model_api_key_decrypt_failed",
                "detail": str(e)[:220],
            }, 400
        envelope = existing_envelope
        api_key_hint = str(existing.get("api_key_hint") or "saved key")

    try:
        test = provider_client.test_provider_key(provider_client.ProviderConfig(provider, model, provider_key, base_url))
    except provider_client.ProviderError as e:
        # Log enough to triage a user-reported "key won't validate" without ever
        # logging the raw key: the failure detail (e.g. provider_http_404 for a
        # bad model name, or 401/429 for a bad/quota'd key) only lives here.
        print(
            f"[model_api:{store.user_id}] setup FAILED provider={provider} "
            f"model={model} status_code={e.status_code} detail={str(e)[:160]}"
        )
        return {
            "error": "provider_test_failed",
            "detail": str(e),
            "status_code": e.status_code,
        }, 400

    # For openai_compatible relays, probe once whether the relay implements the
    # OpenAI Responses API. codex speaks the Responses wire; the in-CVM LiteLLM
    # gateway passes that straight through when the relay supports /v1/responses
    # (preserving codex's tool loop) and otherwise forces the chat-completions
    # bridge. We persist the answer so the supervisor picks the right transport
    # without re-probing every tick. Only openai_compatible uses the flag.
    supports_responses = False
    if provider == "openai_compatible":
        supports_responses = provider_client.probe_responses_support(
            provider_client.ProviderConfig(provider, model, provider_key, base_url)
        )
        print(
            f"[model_api:{store.user_id}] openai_compatible /responses probe -> "
            f"supports={supports_responses} base_url={base_url}"
        )

    config_doc = {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "supports_responses": supports_responses,
        "api_key_hint": api_key_hint,
        "api_key_envelope": envelope,
        "test_status": "ok",
        "last_test_at": core_util._now_iso(),
        "last_test_usage": test.get("usage") or {},
        "privacy_mode": "tdx_cvm_backend_runtime_option_a",
    }
    if reasoning_effort is not None:
        config_doc["reasoning_effort"] = reasoning_effort
    config = hosted_config_store._save_model_api_config(store, config_doc)
    hosted_config_store._ensure_model_api_runtime_profile(store, config, touch=True)
    accounts_onboarding._save_onboarding_route(store, "model_api")
    print(f"[model_api:{store.user_id}] setup provider={provider} model={model}")

    # openai_compatible 中转不实现 /v1/responses → LiteLLM 强制 chat-completions
    # 桥接 → mangle codex 工具循环 → 记忆/工具静默不可靠(rc=0 但工具从不真调,
    # 无限"我再试一次")。配置期就能预知,双写:
    #   ① setup 响应带 warnings → 设置页保存后当场显示(不必等通知中心页)
    #   ② 通知中心 emit → 持久化,通知中心页/其它端也能看到
    # 换到支持 /v1/responses 的中转(或非 openai_compatible provider)时 resolve。
    warnings: list[dict] = []
    try:
        from notices import core as notices_core
        from notices import catalog as notices_catalog
        _ec = "responses_unsupported"
        if provider == "openai_compatible" and not supports_responses:
            _blame = notices_catalog.blame_for(_ec)
            _text = notices_catalog.user_text_for(_ec)
            warnings.append({
                "error_class": _ec, "blame": _blame,
                "severity": "warning", "user_text": _text,
            })
            notices_core.emit(
                store, source="model_api", error_class=_ec,
                blame=_blame, severity="warning", user_text=_text,
                detail=f"probe /v1/responses -> supported=False (base_url={base_url})",
                dedupe_key=f"model_api:{_ec}")
        else:
            notices_core.resolve(store, f"model_api:{_ec}")
    except Exception:
        pass  # 扇出绝不影响 setup 主职责(emit/resolve 本身已 never-raise,这是双保险)

    resp = {"status": "configured", "config": hosted_config_store._public_model_api_config(config)}
    if warnings:
        resp["warnings"] = warnings
    return resp, 200


def model_api_get(store) -> tuple[dict, int]:
    return {"config": hosted_config_store._public_model_api_config(hosted_config_store._load_model_api_config(store))}, 200


def model_api_set_hosting(store) -> tuple[dict, int]:
    """报告该用户派生的 agent driver。AGENT 由 provider 自动派生，配了即托管；
    本端点不再有 enable/disable 开关（保留以兼容旧 client）。"""
    config = hosted_config_store._load_model_api_config(store)
    if not config:
        return {"error": "model_api_not_configured"}, 404
    try:
        driver = agent_runtime_cutover.resolve_driver(config)
    except agent_runtime_cutover.UnsupportedProviderError:
        return {"error": "provider_not_hostable"}, 409
    print(f"[model_api:{store.user_id}] provider={config.get('provider')} -> driver={driver}")
    return {
        "status": "ok",
        "enabled": True,
        "driver": driver,
        "config": hosted_config_store._public_model_api_config(config),
    }, 200


def model_api_key_envelope(store) -> tuple[dict, int]:
    """Return the caller's OWN ``api_key_envelope`` ciphertext.

    Lets the agent-runner supervisor (authenticating with the user's API key)
    self-fetch the provider-key envelope and enclave-decrypt it JIT, instead of a
    static roster carrying per-user secrets. The envelope is ciphertext the server
    cannot decrypt; only the enclave can — so this never exposes the provider key."""
    config = hosted_config_store._load_model_api_config(store)
    envelope = (config or {}).get("api_key_envelope")
    if not isinstance(envelope, dict):
        return {"error": "model_api_key_envelope_missing"}, 404
    return {"api_key_envelope": envelope}, 200


def model_api_test(store, *, api_key: str | None) -> tuple[dict, int]:
    config = hosted_config_store._load_model_api_config(store)
    if not config:
        return {"error": "model_api_not_configured"}, 404
    runtime = hosted_config_store._load_runtime_provider_config(store, api_key)
    if isinstance(runtime, tuple):
        _, err = runtime
        config["test_status"] = "failed"
        config["last_test_error"] = err.get("error", "unknown")
        hosted_config_store._save_model_api_config(store, config)
        return err, 400
    try:
        test = provider_client.test_provider_key(runtime)
    except provider_client.ProviderError as e:
        config["test_status"] = "failed"
        config["last_test_error"] = str(e)[:240]
        hosted_config_store._save_model_api_config(store, config)
        return {
            "error": "provider_test_failed",
            "detail": str(e),
            "status_code": e.status_code,
        }, 400
    config["test_status"] = "ok"
    config["last_test_at"] = core_util._now_iso()
    config["last_test_error"] = ""
    config["last_test_usage"] = test.get("usage") or {}
    hosted_config_store._save_model_api_config(store, config)
    hosted_config_store._ensure_model_api_runtime_profile(store, config, touch=True)
    print(f"[model_api:{store.user_id}] test ok provider={config.get('provider')} model={config.get('model')}")
    return {"status": "ok", "config": hosted_config_store._public_model_api_config(config)}, 200


def model_api_delete(store) -> tuple[dict, int]:
    deleted = db.delete_blob(store.user_id, "model_api")
    db.delete_blob(store.user_id, hosted_config_store.MODEL_API_RUNTIME_BLOB)
    # 配置没了,任何 config 期发出的 model_api 警告(如 responses_unsupported)也随之
    # 作废——否则 /v1/notices 会为一个已不存在的 provider 一直显示活跃警告。
    try:
        from notices import core as notices_core
        notices_core.resolve(store, "model_api:")
    except Exception:
        pass  # 扇出绝不影响 delete 主职责
    print(f"[model_api:{store.user_id}] deleted={deleted}")
    return {"deleted": deleted}, 200


def _model_api_recap_status(store: UserStore) -> dict:
    latest = hosted_turn._model_api_latest_recap_job(store)
    with hosted_turn._model_api_recap_active_lock:
        active = store.user_id in hosted_turn._model_api_recap_active_users
    if not latest:
        return {"status": "idle", "active": active}
    status = str(latest.get("status") or "idle")
    if active and status not in {"failed", "completed", "skipped"}:
        status = "running"
    return {
        "status": status,
        "active": active,
        "job_id": latest.get("job_id", ""),
        "mode": latest.get("mode", ""),
        "progress": latest.get("progress", 0),
        "updated_at": latest.get("completed_at") or latest.get("created_at") or "",
    }


def model_api_runtime_status(store, *, api_key: str | None) -> tuple[dict, int]:
    config = hosted_config_store._load_model_api_config(store)
    if not config:
        return {
            "configured": False,
            "runtime_mode": hosted_config_store.MODEL_API_RUNTIME_MODE,
            "runtime_version": hosted_config_store.MODEL_API_RUNTIME_VERSION,
            "recap_status": "idle",
            "memory_quality_warning": None,
        }, 200
    profile = hosted_config_store._ensure_model_api_runtime_profile(store, config) or {}
    scan = hosted_turn._model_api_memory_quality_scan(store, api_key=api_key, max_cards=120, fast=True)
    warning = scan.get("warning")
    if warning != profile.get("memory_quality_warning"):
        profile = hosted_config_store._patch_model_api_runtime_profile(store, {"memory_quality_warning": warning}) or profile
    latest_trace = hosted_config_store._latest_model_api_action_trace(store)
    recap = _model_api_recap_status(store)
    return {
        "configured": True,
        "runtime_mode": profile.get("runtime_mode") or hosted_config_store.MODEL_API_RUNTIME_MODE,
        "runtime_version": int(profile.get("runtime_version") or hosted_config_store.MODEL_API_RUNTIME_VERSION),
        "tool_action_enabled": bool(profile.get("tool_action_enabled", True)),
        "provider": config.get("provider", ""),
        "model": config.get("model", ""),
        "recap_status": recap.get("status", "idle"),
        "recap": recap,
        "memory_quality_warning": warning,
        "memory_quality": {
            "scanned": scan.get("scanned", 0),
            "issue_count": scan.get("issue_count", 0),
            "noisy_count": scan.get("noisy_count", 0),
            "duplicate_count": scan.get("duplicate_count", 0),
        },
        "last_action_trace_id": profile.get("last_action_trace_id", ""),
        "last_action_trace_at": profile.get("last_action_trace_at", ""),
        "last_action_trace_status": (latest_trace or {}).get("status", ""),
        "last_runtime_error": profile.get("last_runtime_error", ""),
        "last_runtime_error_class": profile.get("last_runtime_error_class", ""),
    }, 200


def model_api_memory_repair(store, payload: dict, *, api_key: str | None, testing: bool) -> tuple[dict, int]:
    mode = str(payload.get("mode") or "dry_run").strip().lower()
    if mode not in {"dry_run", "apply"}:
        return {"error": "mode must be dry_run or apply"}, 400
    archive_old = bool(payload.get("archive_old", True))
    scan = hosted_turn._model_api_memory_quality_scan(store, api_key=api_key, max_cards=2000)
    noisy_count = int(scan.get("noisy_count") or 0)
    new_cards_planned = max(6, min(30, noisy_count)) if noisy_count else 0
    preview = {
        "old_cards_detected": noisy_count,
        "issue_count": int(scan.get("issue_count") or 0),
        "duplicate_count": int(scan.get("duplicate_count") or 0),
        "new_cards_planned": new_cards_planned,
        "noisy_ids": scan.get("noisy_ids", [])[:80],
        "issues": scan.get("issues", [])[:20],
    }
    hosted_config_store._patch_model_api_runtime_profile(store, {
        "memory_quality_warning": scan.get("warning"),
        "last_memory_quality_scan_at": core_util._now_iso(),
    })
    if mode == "dry_run":
        return {
            "status": "completed",
            "mode": "dry_run",
            "preview": preview,
            "memory_quality": scan,
        }, 200
    if not preview["old_cards_detected"]:
        return {
            "status": "skipped",
            "mode": "apply",
            "reason": "no_noisy_memory_cards_detected",
            "preview": preview,
        }, 200

    runtime = hosted_config_store._load_runtime_provider_config(store, api_key)
    if isinstance(runtime, tuple):
        _, err = runtime
        return err, 400

    job = memory_service._append_memory_capture_job(store, {
        "mode": "repair",
        "status": "queued",
        "progress": 0,
        "old_cards_detected": preview["old_cards_detected"],
        "new_cards_planned": preview["new_cards_planned"],
        "repair_noisy_ids": preview["noisy_ids"],
        "archive_old": archive_old,
    })
    run_sync = bool(payload.get("synchronous") or payload.get("sync") or testing)
    if run_sync:
        hosted_turn._run_model_api_memory_repair_job(
            store,
            api_key,
            runtime,
            job["job_id"],
            noisy_ids=preview["noisy_ids"],
            archive_old=archive_old,
        )
        jobs = db.log_read(store.user_id, "memory_capture_jobs", limit=20)
        latest = next((item for item in reversed(jobs) if item.get("job_id") == job["job_id"]), job)
        return {
            "status": latest.get("status", "completed"),
            "mode": "apply",
            "job_id": job["job_id"],
            "job": latest,
            "preview": preview,
        }, 200

    thread = threading.Thread(
        target=hosted_turn._run_model_api_memory_repair_job,
        args=(store, api_key, runtime, job["job_id"]),
        kwargs={"noisy_ids": preview["noisy_ids"], "archive_old": archive_old},
        daemon=True,
    )
    thread.start()
    return {
        "status": "queued",
        "mode": "apply",
        "job_id": job["job_id"],
        "job": job,
        "preview": preview,
    }, 202


def state_receipts(store, limit_raw) -> tuple[dict, int]:
    try:
        limit = min(max(int(limit_raw), 1), 100)
    except (TypeError, ValueError):
        return {"error": "invalid limit"}, 400
    return {
        "receipts": hosted_turn._load_state_receipts(store, limit=limit),
        "pending": [
            {
                "id": item.get("id", ""),
                "created_at": item.get("created_at", ""),
                "expires_at": item.get("expires_at", 0),
                "action": ((item.get("runtime_action") or {}).get("runtime_type") or ""),
                "confidence": (item.get("runtime_action") or {}).get("confidence", 0),
            }
            for item in hosted_turn._state_pending_items(store)
        ],
    }, 200


def memory_capture_jobs(store, limit_raw) -> tuple[dict, int]:
    try:
        limit = min(max(int(limit_raw), 1), 100)
    except (TypeError, ValueError):
        return {"error": "invalid limit"}, 400
    jobs = db.log_read(store.user_id, "memory_capture_jobs", limit=limit)
    jobs.sort(key=lambda item: float(item.get("ts") or 0), reverse=True)
    with hosted_turn._model_api_recap_active_lock:
        active_recap = store.user_id in hosted_turn._model_api_recap_active_users
    return {
        "jobs": jobs,
        "active_recap": active_recap,
    }, 200
