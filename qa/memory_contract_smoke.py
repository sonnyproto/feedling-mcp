#!/usr/bin/env python3
"""Deterministic live memory-contract qualification for one synthetic account.

The account is provisioned and cleaned up by the caller.  This program only
accepts a private one-profile session manifest, drives the deployed memory API,
and writes a bounded receipt that contains no account credential, memory id,
plaintext card, ciphertext, or raw response.

The first eight checks exercise public live behavior, including the resident
capture executor with deterministic agent output.  The last two exercise
the deployed legacy-migration implementation through its authenticated API.
When the deployment's migration kill switch is off, those checks are marked
NOT_EXERCISED and the process exits 2 (UNVERIFIED); they are never reported as
passing without evidence.
"""

from __future__ import annotations

import argparse
import base64
import importlib
import json
import os
import re
import secrets
import stat
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from jsonschema import Draft202012Validator


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "backend"))

from content_encryption import build_envelope  # noqa: E402
from qa.cot_delivery_probe import load_profile_session  # noqa: E402
from tools.provider_smoke import crypto as smoke_crypto  # noqa: E402
from tools.provider_smoke.client import Session, SmokeClient  # noqa: E402


RECEIPT_SCHEMA_VERSION = 1
DEFAULT_SCHEMA_PATH = Path(__file__).with_name("schemas") / "memory-contract-receipt.schema.json"
EXPECTED_PROFILE_ID = "memory-contract"
DEFAULT_BASE_URL = "https://test-api.feedling.app"

_SHARED_SUMMARY = "QA memory contract: shared v1 card"
_SHARED_CONTENT = "Synthetic shared memory for deterministic index and fetch qualification."
_SUPERSEDE_SUMMARY = "QA memory contract: corrected shared v1 card"
_SUPERSEDE_CONTENT = "Synthetic corrected memory that must replace the original card."
_LEGACY_TITLE = "QA legacy migration card"
_LEGACY_DESCRIPTION = "Synthetic legacy content that must migrate in place."
_MIGRATED_SUMMARY = "QA migrated card"
_MIGRATED_CONTENT = "Synthetic v1 content written while preserving the legacy card id."
_CAS_WINNER_SUMMARY = "QA CAS winning update"
_CAS_WINNER_CONTENT = "Synthetic winning update that a stale writer must not overwrite."
_CAS_STALE_SUMMARY = "QA CAS stale update"
_CAS_STALE_CONTENT = "Synthetic stale update that must be rejected."
_CAS_SENTINEL_SUMMARY = "QA concurrent sentinel"
_CAS_SENTINEL_CONTENT = "Synthetic concurrent card that must survive an in-place update."
_CAPTURE_SUMMARY = "QA capture contract: preferred constellation is Lyra"
_CAPTURE_CONTENT = (
    "The synthetic QA user explicitly asked the agent to remember that their "
    "preferred constellation is Lyra."
)
_CAPTURE_BUCKET = "QA capture contract"
_CAPTURE_THREADS = ["Lyra", "constellation preference"]
_CAPTURE_USER_TEXT = (
    "Please remember this durable preference: my preferred constellation is Lyra."
)
_CHITCHAT_USER_TEXT = "Hello for a moment; this is disposable chitchat with nothing to remember."
_DUPLICATE_USER_TEXT = (
    "I am repeating the same fact: my preferred constellation is still Lyra."
)
_CAPTURE_CONSUMER_ID = "qa-memory-contract"

_ENCLAVE_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CHECKS: tuple[tuple[str, str], ...] = (
    ("fresh_empty_recall", "live_api"),
    ("encrypted_v1_index_fetch", "live_api"),
    ("quiet_window_capture_write", "live_api"),
    ("route_chat_message_trace", "live_api"),
    ("capture_noop_disposable_chitchat", "live_api"),
    ("duplicate_fact_no_growth", "live_api"),
    ("local_only_exclusion", "live_api"),
    ("supersede_visibility", "live_api"),
    ("legacy_migration_stable_id", "deployed_backend_contract"),
    ("stale_cas_preserves_concurrent_updates", "deployed_backend_contract"),
)


class _MemoryClient(Protocol):
    def _req(
        self,
        method: str,
        path: str,
        *,
        api_key: str | None = None,
        body: dict | None = None,
        attempts: int = 5,
        read_timeout: float = 45,
    ) -> tuple[int, dict]: ...


class MemoryContractError(RuntimeError):
    """One fixed-code qualification failure; never includes a raw response."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _MigrationUnavailable(RuntimeError):
    pass


@dataclass
class _Context:
    client: _MemoryClient
    session: Session
    base_url: str = DEFAULT_BASE_URL
    checks: list[dict[str, Any]] = field(default_factory=list)
    enclave_pk: bytes = b""
    shared_id: str = ""
    captured_id: str = ""
    trace_message_id: str = ""
    capture_executor: Callable[["_Context", dict[str, Any], str], Mapping[str, Any]] | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request(
    context: _Context,
    method: str,
    path: str,
    *,
    body: dict | None = None,
    allowed_statuses: tuple[int, ...] = (200,),
    failure_code: str,
) -> dict[str, Any]:
    try:
        status, response = context.client._req(
            method,
            path,
            api_key=context.session.api_key,
            body=body,
        )
    except Exception:
        raise MemoryContractError("API_REQUEST_FAILED") from None
    if status not in allowed_statuses or not isinstance(response, dict):
        raise MemoryContractError(failure_code)
    return response


def _contains_text(value: Any, text: str) -> bool:
    if isinstance(value, str):
        return text in value
    if isinstance(value, Mapping):
        return any(
            _contains_text(key, text) or _contains_text(item, text)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_text(item, text) for item in value)
    return False


def _items(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = body.get("items")
    return [dict(item) for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []


def _result(body: Mapping[str, Any], *, failure_code: str) -> dict[str, Any]:
    results = body.get("results")
    if body.get("status") != "ok" or not isinstance(results, list) or len(results) != 1:
        raise MemoryContractError(failure_code)
    result = results[0]
    if not isinstance(result, Mapping):
        raise MemoryContractError(failure_code)
    return dict(result)


def _memory_id(result: Mapping[str, Any], *, failure_code: str) -> str:
    memory = result.get("memory")
    memory_id = str(memory.get("id") or "") if isinstance(memory, Mapping) else ""
    if not memory_id:
        raise MemoryContractError(failure_code)
    return memory_id


def _record(context: _Context, check_id: str, layer: str, observations: dict[str, Any]) -> None:
    context.checks.append(
        {
            "id": check_id,
            "layer": layer,
            "status": "PASS",
            "failure_code": "NONE",
            "observations": observations,
        }
    )


def _envelope(
    context: _Context,
    inner: Mapping[str, Any],
    *,
    visibility: str,
    memory_type: str = "fact",
    source: str,
    item_id: str | None = None,
) -> dict[str, Any]:
    envelope = build_envelope(
        plaintext=json.dumps(
            dict(inner), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8"),
        owner_user_id=context.session.user_id,
        user_pk_bytes=context.session.pk,
        enclave_pk_bytes=context.enclave_pk if visibility == "shared" else None,
        visibility=visibility,
        item_id=item_id,
    )
    envelope.update(
        {
            "type": memory_type,
            "occurred_at": _utc_now(),
            "source": source,
            "importance": 0.7,
            "pulse": 0.5,
        }
    )
    return envelope


def _add_envelope(
    context: _Context,
    envelope: dict[str, Any],
    *,
    failure_code: str,
) -> str:
    body = _request(
        context,
        "POST",
        "/v1/memory/add",
        body={"envelope": envelope},
        allowed_statuses=(201,),
        failure_code=failure_code,
    )
    moment = body.get("moment")
    memory_id = str(moment.get("id") or "") if isinstance(moment, Mapping) else ""
    if body.get("status") != "created" or memory_id != str(envelope.get("id") or ""):
        raise MemoryContractError(failure_code)
    return memory_id


def _get_raw(context: _Context, memory_id: str, *, failure_code: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"id": memory_id})
    body = _request(
        context,
        "GET",
        f"/v1/memory/get?{query}",
        failure_code=failure_code,
    )
    moment = body.get("moment")
    if not isinstance(moment, Mapping) or str(moment.get("id") or "") != memory_id:
        raise MemoryContractError(failure_code)
    return dict(moment)


def _check_fresh_empty(context: _Context) -> dict[str, Any]:
    index = _request(
        context,
        "POST",
        "/v1/memory/index",
        body={"limit": 20},
        failure_code="FRESH_EMPTY_RECALL_FAILED",
    )
    missing_id = f"mom_qa_missing_{secrets.token_hex(8)}"
    fetch = _request(
        context,
        "POST",
        "/v1/memory/fetch",
        body={"ids": [missing_id]},
        failure_code="FRESH_EMPTY_RECALL_FAILED",
    )
    index_items = _items(index)
    fetch_items = _items(fetch)
    missing_ids = fetch.get("missing_ids")
    unavailable_ids = fetch.get("unavailable_ids")
    if (
        index_items
        or index.get("user_card_count") != 0
        or fetch_items
        or not isinstance(missing_ids, list)
        or missing_ids != [missing_id]
        or not isinstance(unavailable_ids, list)
        or unavailable_ids
    ):
        raise MemoryContractError("FRESH_ACCOUNT_NOT_EMPTY")
    return {"index_count": 0, "fetch_count": 0, "missing_count": 1}


def _load_enclave_material(context: _Context) -> None:
    whoami = _request(
        context,
        "GET",
        "/v1/users/whoami",
        failure_code="KEY_MATERIAL_UNAVAILABLE",
    )
    enclave_hex = str(whoami.get("enclave_content_public_key_hex") or "")
    try:
        advertised_user_pk = base64.b64decode(
            str(whoami.get("public_key") or ""), validate=True
        )
    except Exception:
        advertised_user_pk = b""
    if (
        whoami.get("user_id") != context.session.user_id
        or advertised_user_pk != context.session.pk
        or not _ENCLAVE_KEY_RE.fullmatch(enclave_hex)
    ):
        raise MemoryContractError("KEY_MATERIAL_UNAVAILABLE")
    context.enclave_pk = bytes.fromhex(enclave_hex)


def _check_encrypted_v1(context: _Context) -> dict[str, Any]:
    _load_enclave_material(context)
    envelope = _envelope(
        context,
        {
            "summary": _SHARED_SUMMARY,
            "content": _SHARED_CONTENT,
            "bucket": "QA contract",
            "threads": ["memory qualification"],
        },
        visibility="shared",
        source="qa_memory_contract_shared",
    )
    memory_id = _add_envelope(
        context, envelope, failure_code="ENCRYPTED_WRITE_FAILED"
    )
    context.shared_id = memory_id
    raw = _get_raw(context, memory_id, failure_code="ENCRYPTED_STORAGE_FAILED")
    encrypted_at_rest = all(
        bool(raw.get(field)) for field in ("body_ct", "nonce", "K_user", "K_enclave")
    )
    if (
        not encrypted_at_rest
        or raw.get("visibility") != "shared"
        or any(field in raw for field in ("summary", "content", "title", "description"))
        or _contains_text(raw, _SHARED_SUMMARY)
        or _contains_text(raw, _SHARED_CONTENT)
    ):
        raise MemoryContractError("ENCRYPTED_STORAGE_FAILED")

    index = _request(
        context,
        "POST",
        "/v1/memory/index",
        body={"limit": 20},
        failure_code="INDEX_FETCH_FAILED",
    )
    indexed = [item for item in _items(index) if item.get("id") == memory_id]
    fetch = _request(
        context,
        "POST",
        "/v1/memory/fetch",
        body={"ids": [memory_id]},
        failure_code="INDEX_FETCH_FAILED",
    )
    fetched = [item for item in _items(fetch) if item.get("id") == memory_id]
    if (
        len(indexed) != 1
        or indexed[0].get("summary") != _SHARED_SUMMARY
        or len(fetched) != 1
        or fetched[0].get("summary") != _SHARED_SUMMARY
        or fetched[0].get("content") != _SHARED_CONTENT
        or fetch.get("missing_ids") != []
        or fetch.get("unavailable_ids") != []
    ):
        raise MemoryContractError("INDEX_FETCH_FAILED")
    return {
        "stored_record_count": 1,
        "index_count": 1,
        "fetch_count": 1,
        "encrypted_at_rest": True,
        "round_trip_verified": True,
    }


def _post_chat_message(
    context: _Context, text: str, *, failure_code: str
) -> str:
    if not context.enclave_pk:
        raise MemoryContractError(failure_code)
    envelope = build_envelope(
        plaintext=text.encode("utf-8"),
        owner_user_id=context.session.user_id,
        user_pk_bytes=context.session.pk,
        enclave_pk_bytes=context.enclave_pk,
        visibility="shared",
    )
    body = _request(
        context,
        "POST",
        "/v1/chat/message",
        body={"envelope": envelope, "content_type": "text"},
        failure_code=failure_code,
    )
    message_id = str(body.get("id") or "")
    timestamp = body.get("ts")
    if (
        message_id != str(envelope.get("id") or "")
        or isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or timestamp <= 0
    ):
        raise MemoryContractError(failure_code)
    return message_id


def _memory_index(context: _Context, *, failure_code: str) -> list[dict[str, Any]]:
    body = _request(
        context,
        "POST",
        "/v1/memory/index",
        body={"limit": 100},
        failure_code=failure_code,
    )
    raw_items = body.get("items")
    if not isinstance(raw_items, list) or any(
        not isinstance(item, Mapping) for item in raw_items
    ):
        raise MemoryContractError(failure_code)
    return [dict(item) for item in raw_items]


def _memory_vocabulary(
    context: _Context, *, failure_code: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    buckets_body = _request(
        context,
        "GET",
        "/v1/memory/buckets",
        failure_code=failure_code,
    )
    threads_body = _request(
        context,
        "GET",
        "/v1/memory/threads",
        failure_code=failure_code,
    )
    buckets = buckets_body.get("buckets")
    threads = threads_body.get("threads")
    if (
        not isinstance(buckets, list)
        or not isinstance(threads, list)
        or any(not isinstance(item, str) for item in [*buckets, *threads])
    ):
        raise MemoryContractError(failure_code)
    return tuple(sorted(buckets)), tuple(sorted(threads))


def _decrypted_chat_history(
    context: _Context,
    since: float,
    limit: int = 20,
    include_image_body: bool = True,
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "since": max(0.0, float(since)),
            "limit": max(1, min(int(limit), 200)),
            "include_image_body": "true" if include_image_body else "false",
        }
    )
    body = _request(
        context,
        "GET",
        f"/v1/chat/history?{query}",
        failure_code="CAPTURE_HISTORY_FAILED",
    )
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise MemoryContractError("CAPTURE_HISTORY_FAILED")
    decrypted: list[dict[str, Any]] = []
    for raw in messages:
        if not isinstance(raw, Mapping):
            raise MemoryContractError("CAPTURE_HISTORY_FAILED")
        item = dict(raw)
        if item.get("body_ct"):
            try:
                item["content"] = smoke_crypto.decrypt_reply(
                    item, context.session.sk, context.session.pk
                )
            except Exception:
                raise MemoryContractError("CAPTURE_HISTORY_FAILED") from None
        decrypted.append(item)
    return decrypted


def _native_capture_executor(
    context: _Context, job: dict[str, Any], agent_reply: str
) -> Mapping[str, Any]:
    """Run the checked-in resident capture path with deterministic agent output.

    All live mutations still go through the account-scoped API client.  The
    patched callbacks only replace the provider call and enclave decrypt proxy,
    so the native prompt parser, action builder, crypto, claim, status, and
    memory-action path are exercised without a provider credential.
    """

    previous_environment = {
        name: os.environ.get(name) for name in ("FEEDLING_API_URL", "FEEDLING_API_KEY")
    }
    os.environ["FEEDLING_API_URL"] = context.base_url.rstrip("/")
    os.environ["FEEDLING_API_KEY"] = context.session.api_key
    try:
        consumer = importlib.import_module("tools.chat_resident_consumer")
    except Exception:
        raise MemoryContractError("CAPTURE_EXECUTOR_FAILED") from None
    finally:
        for name, previous in previous_environment.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous

    terminal: dict[str, Any] = {}

    def claim(job_id: str) -> bool:
        body = _request(
            context,
            "POST",
            f"/v1/proactive/jobs/{urllib.parse.quote(job_id, safe='')}/claim",
            body={"consumer_id": _CAPTURE_CONSUMER_ID},
            failure_code="CAPTURE_EXECUTOR_FAILED",
        )
        claimed_job = body.get("job")
        if (
            body.get("claimed") is not True
            or not isinstance(claimed_job, Mapping)
            or claimed_job.get("job_id") != job_id
            or claimed_job.get("status") != "claimed"
        ):
            raise MemoryContractError("CAPTURE_EXECUTOR_FAILED")
        return True

    def update_status(
        job_id: str,
        status: str,
        reason: str = "",
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "status": status,
            "reason": reason,
            "consumer_id": _CAPTURE_CONSUMER_ID,
        }
        if isinstance(extra, dict):
            payload.update(extra)
        body = _request(
            context,
            "POST",
            f"/v1/proactive/jobs/{urllib.parse.quote(job_id, safe='')}/status",
            body=payload,
            failure_code="CAPTURE_EXECUTOR_FAILED",
        )
        updated_job = body.get("job")
        if (
            not isinstance(updated_job, Mapping)
            or updated_job.get("job_id") != job_id
            or updated_job.get("status") != status
        ):
            raise MemoryContractError("CAPTURE_EXECUTOR_FAILED")
        if status in {"completed", "failed", "skipped"}:
            terminal.clear()
            terminal.update(dict(updated_job))

    def execute_actions(actions: list[dict[str, Any]]) -> dict[str, Any]:
        body = _request(
            context,
            "POST",
            "/v1/memory/actions",
            body={"actions": actions},
            failure_code="CAPTURE_EXECUTOR_FAILED",
        )
        if body.get("status") not in {"ok", "created", "replaced"} or not isinstance(
            body.get("results"), list
        ):
            raise MemoryContractError("CAPTURE_EXECUTOR_FAILED")
        return body

    def capture_envelope(
        card: dict[str, Any],
        *,
        occurred_at: str,
        source: str = "memory_capture",
        item_id: str = "",
    ) -> dict[str, Any]:
        envelope = _envelope(
            context,
            {
                "summary": str(card.get("summary") or ""),
                "content": str(card.get("content") or ""),
                "bucket": str(card.get("bucket") or ""),
                "threads": list(card.get("threads") or []),
            },
            visibility="shared",
            memory_type=str(card.get("type") or "event"),
            source=source,
            item_id=item_id or None,
        )
        envelope.update(
            {
                "occurred_at": occurred_at,
                "importance": float(card.get("importance") or 0),
                "pulse": float(card.get("pulse") or 0),
                "last_referenced_at": occurred_at,
                "anchor_memory_ids": [],
            }
        )
        return envelope

    def call_agent(_prompt: str, *_args: Any, raw_text: bool = False, **_kwargs: Any) -> str:
        if raw_text is not True:
            raise MemoryContractError("CAPTURE_EXECUTOR_FAILED")
        return agent_reply

    patches: dict[str, Any] = {
        "claim_proactive_job": claim,
        "update_proactive_job_status": update_status,
        "execute_memory_actions": execute_actions,
        "get_decrypted_history": lambda since, limit=20, include_image_body=True: _decrypted_chat_history(
            context, since, limit, include_image_body
        ),
        "call_agent": call_agent,
        "_capture_build_envelope": capture_envelope,
        "_capture_identity_context": lambda: ({}, "QA Agent", "QA User", "{}"),
        "_capture_memory_terms_context": lambda: ("[]", "[]"),
        "_note_agent_turn_success": lambda: None,
        "_notify_agent_turn_failure": lambda *_args, **_kwargs: None,
    }
    originals = {name: getattr(consumer, name) for name in patches}
    seen_before = set(consumer._seen_ids)
    seen_order_before = list(consumer._seen_ids_order)
    try:
        for name, value in patches.items():
            setattr(consumer, name, value)
        consumer._seen_ids.clear()
        consumer._seen_ids_order.clear()
        consumer._process_capture_jobs([job])
    except MemoryContractError:
        raise
    except Exception:
        raise MemoryContractError("CAPTURE_EXECUTOR_FAILED") from None
    finally:
        for name, value in originals.items():
            setattr(consumer, name, value)
        consumer._seen_ids.clear()
        consumer._seen_ids.update(seen_before)
        consumer._seen_ids_order[:] = seen_order_before
    if not terminal:
        raise MemoryContractError("CAPTURE_EXECUTOR_FAILED")
    return terminal


def _run_capture_job(
    context: _Context,
    *,
    agent_reply: str,
    now_offset_sec: float,
    expected_capture_status: str,
    expected_cards_added: int,
    expected_cards_superseded: int,
    failure_code: str,
) -> dict[str, Any]:
    tick = _request(
        context,
        "POST",
        "/v1/capture/tick",
        body={"now": time.time() + now_offset_sec},
        failure_code=failure_code,
    )
    job_summary = tick.get("job")
    if (
        tick.get("enqueued") is not True
        or tick.get("reason") != "enqueued"
        or not isinstance(job_summary, Mapping)
        or job_summary.get("job_kind") != "memory_capture"
        or job_summary.get("status") != "pending"
        or job_summary.get("trigger") != "quiet_timeout"
    ):
        raise MemoryContractError(failure_code)
    job_id = str(job_summary.get("job_id") or "")
    if not job_id:
        raise MemoryContractError(failure_code)

    poll_query = urllib.parse.urlencode({"since": 0, "timeout": 0, "limit": 100})
    polled = _request(
        context,
        "GET",
        f"/v1/proactive/jobs/poll?{poll_query}",
        failure_code=failure_code,
    )
    jobs = polled.get("jobs")
    matches = (
        [dict(item) for item in jobs if isinstance(item, Mapping) and item.get("job_id") == job_id]
        if isinstance(jobs, list)
        else []
    )
    if (
        len(matches) != 1
        or matches[0].get("job_kind") != "memory_capture"
        or matches[0].get("status") != "pending"
        or matches[0].get("trigger") != "quiet_timeout"
    ):
        raise MemoryContractError(failure_code)

    executor = context.capture_executor or _native_capture_executor
    try:
        terminal = executor(context, matches[0], agent_reply)
    except MemoryContractError:
        raise
    except Exception:
        raise MemoryContractError("CAPTURE_EXECUTOR_FAILED") from None
    capture_result = terminal.get("capture_result")
    if (
        terminal.get("status") != "completed"
        or not isinstance(capture_result, Mapping)
        or capture_result.get("status") != expected_capture_status
        or terminal.get("cards_added") != expected_cards_added
        or terminal.get("cards_superseded") != expected_cards_superseded
    ):
        raise MemoryContractError(failure_code)
    if expected_capture_status == "ok" and capture_result.get("cards") != 1:
        raise MemoryContractError(failure_code)
    if expected_capture_status == "noop" and (
        capture_result.get("reason") != "nothing_worth_keeping"
        or terminal.get("noop_reason") != "nothing_worth_keeping"
    ):
        raise MemoryContractError(failure_code)
    return {
        "capture_job_count": 1,
        "cards_added": expected_cards_added,
        "cards_superseded": expected_cards_superseded,
        "quiet_window_enqueued": True,
        "capture_noop": expected_capture_status == "noop",
    }


def _check_quiet_window_capture_write(context: _Context) -> dict[str, Any]:
    before = _memory_index(context, failure_code="CAPTURE_WRITE_FAILED")
    enabled = _request(
        context,
        "POST",
        "/v1/debug/trace/enable",
        body={"enabled": True},
        failure_code="FLOW_TRACE_FAILED",
    )
    if enabled.get("enabled") is not True or enabled.get("deploy_enabled") is not True:
        raise MemoryContractError("FLOW_TRACE_FAILED")
    cleared = _request(
        context,
        "DELETE",
        "/v1/debug/trace",
        failure_code="FLOW_TRACE_FAILED",
    )
    if cleared.get("status") != "ok":
        raise MemoryContractError("FLOW_TRACE_FAILED")
    context.trace_message_id = _post_chat_message(
        context, _CAPTURE_USER_TEXT, failure_code="CAPTURE_MESSAGE_FAILED"
    )
    reply = json.dumps(
        {
            "cards": [
                {
                    "action": "add",
                    "type": "fact",
                    "target_id": None,
                    "bucket": _CAPTURE_BUCKET,
                    "threads": _CAPTURE_THREADS,
                    "summary": _CAPTURE_SUMMARY,
                    "content": _CAPTURE_CONTENT,
                    "importance": 0.8,
                    "pulse": 0.3,
                }
            ]
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    observations = _run_capture_job(
        context,
        agent_reply=reply,
        now_offset_sec=100_000,
        expected_capture_status="ok",
        expected_cards_added=1,
        expected_cards_superseded=0,
        failure_code="CAPTURE_WRITE_FAILED",
    )
    after = _memory_index(context, failure_code="CAPTURE_WRITE_FAILED")
    matching = [item for item in after if item.get("summary") == _CAPTURE_SUMMARY]
    if len(after) != len(before) + 1 or len(matching) != 1:
        raise MemoryContractError("CAPTURE_WRITE_FAILED")
    context.captured_id = str(matching[0].get("id") or "")
    if not context.captured_id:
        raise MemoryContractError("CAPTURE_WRITE_FAILED")
    fetched = _request(
        context,
        "POST",
        "/v1/memory/fetch",
        body={"ids": [context.captured_id]},
        failure_code="CAPTURE_WRITE_FAILED",
    )
    fetched_items = _items(fetched)
    if (
        len(fetched_items) != 1
        or fetched_items[0].get("summary") != _CAPTURE_SUMMARY
        or fetched_items[0].get("content") != _CAPTURE_CONTENT
        or fetched.get("missing_ids") != []
        or fetched.get("unavailable_ids") != []
    ):
        raise MemoryContractError("CAPTURE_WRITE_FAILED")
    observations.update(
        {
            "before_card_count": len(before),
            "after_card_count": len(after),
            "fetch_count": 1,
            "round_trip_verified": True,
        }
    )
    return observations


def _check_route_chat_message_trace(context: _Context) -> dict[str, Any]:
    if not context.trace_message_id:
        raise MemoryContractError("FLOW_TRACE_FAILED")
    query = urllib.parse.urlencode({"subsystem": "route", "limit": 50})
    trace = _request(
        context,
        "GET",
        f"/v1/debug/trace?{query}",
        failure_code="FLOW_TRACE_FAILED",
    )
    events = trace.get("events")
    matching = (
        [
            event
            for event in events
            if isinstance(event, Mapping)
            and event.get("type") == "chat.message"
            and event.get("trace_id") == context.trace_message_id
            and event.get("turn_id") == context.trace_message_id
            and isinstance(event.get("detail"), Mapping)
            and event["detail"].get("msg_id") == context.trace_message_id
        ]
        if isinstance(events, list)
        else []
    )
    if (
        trace.get("enabled") is not True
        or trace.get("deploy_enabled") is not True
        or len(matching) != 1
    ):
        raise MemoryContractError("FLOW_TRACE_FAILED")
    return {"route_event_count": 1, "route_event_correlated": True}


def _check_capture_noop_disposable_chitchat(context: _Context) -> dict[str, Any]:
    before = _memory_index(context, failure_code="CAPTURE_NOOP_FAILED")
    before_ids = {str(item.get("id") or "") for item in before}
    before_vocabulary = _memory_vocabulary(
        context, failure_code="CAPTURE_NOOP_FAILED"
    )
    _post_chat_message(
        context, _CHITCHAT_USER_TEXT, failure_code="CAPTURE_MESSAGE_FAILED"
    )
    observations = _run_capture_job(
        context,
        agent_reply='{"cards":[]}',
        now_offset_sec=200_000,
        expected_capture_status="noop",
        expected_cards_added=0,
        expected_cards_superseded=0,
        failure_code="CAPTURE_NOOP_FAILED",
    )
    after = _memory_index(context, failure_code="CAPTURE_NOOP_FAILED")
    after_ids = {str(item.get("id") or "") for item in after}
    after_vocabulary = _memory_vocabulary(
        context, failure_code="CAPTURE_NOOP_FAILED"
    )
    if before_ids != after_ids or before_vocabulary != after_vocabulary:
        raise MemoryContractError("CAPTURE_NOOP_FAILED")
    observations.update(
        {
            "before_card_count": len(before),
            "after_card_count": len(after),
            "bucket_vocab_unchanged": True,
            "thread_vocab_unchanged": True,
        }
    )
    return observations


def _check_duplicate_fact_no_growth(context: _Context) -> dict[str, Any]:
    if not context.captured_id:
        raise MemoryContractError("CAPTURE_DEDUP_FAILED")
    before = _memory_index(context, failure_code="CAPTURE_DEDUP_FAILED")
    before_ids = {str(item.get("id") or "") for item in before}
    before_vocabulary = _memory_vocabulary(
        context, failure_code="CAPTURE_DEDUP_FAILED"
    )
    _post_chat_message(
        context, _DUPLICATE_USER_TEXT, failure_code="CAPTURE_MESSAGE_FAILED"
    )
    observations = _run_capture_job(
        context,
        agent_reply='{"cards":[]}',
        now_offset_sec=300_000,
        expected_capture_status="noop",
        expected_cards_added=0,
        expected_cards_superseded=0,
        failure_code="CAPTURE_DEDUP_FAILED",
    )
    after = _memory_index(context, failure_code="CAPTURE_DEDUP_FAILED")
    after_ids = {str(item.get("id") or "") for item in after}
    after_vocabulary = _memory_vocabulary(
        context, failure_code="CAPTURE_DEDUP_FAILED"
    )
    fetched = _request(
        context,
        "POST",
        "/v1/memory/fetch",
        body={"ids": [context.captured_id]},
        failure_code="CAPTURE_DEDUP_FAILED",
    )
    fetched_items = _items(fetched)
    if (
        before_ids != after_ids
        or before_vocabulary != after_vocabulary
        or context.captured_id not in after_ids
        or len(fetched_items) != 1
        or fetched_items[0].get("id") != context.captured_id
        or fetched_items[0].get("summary") != _CAPTURE_SUMMARY
        or fetched_items[0].get("content") != _CAPTURE_CONTENT
    ):
        raise MemoryContractError("CAPTURE_DEDUP_FAILED")
    observations.update(
        {
            "before_card_count": len(before),
            "after_card_count": len(after),
            "fetch_count": 1,
            "bucket_vocab_unchanged": True,
            "thread_vocab_unchanged": True,
            "existing_card_preserved": True,
        }
    )
    return observations


def _check_local_only(context: _Context) -> dict[str, Any]:
    envelope = _envelope(
        context,
        {
            "summary": "QA local-only card",
            "content": "Synthetic local-only content that the enclave must not read.",
            "bucket": "QA contract",
            "threads": [],
        },
        visibility="local_only",
        source="qa_memory_contract_local_only",
    )
    memory_id = _add_envelope(
        context, envelope, failure_code="LOCAL_ONLY_FAILED"
    )
    raw = _get_raw(context, memory_id, failure_code="LOCAL_ONLY_FAILED")
    index = _request(
        context,
        "POST",
        "/v1/memory/index",
        body={"limit": 20},
        failure_code="LOCAL_ONLY_FAILED",
    )
    fetch = _request(
        context,
        "POST",
        "/v1/memory/fetch",
        body={"ids": [memory_id]},
        failure_code="LOCAL_ONLY_FAILED",
    )
    if (
        raw.get("visibility") != "local_only"
        or "K_enclave" in raw
        or any(item.get("id") == memory_id for item in _items(index))
        or any(item.get("id") == memory_id for item in _items(fetch))
        or fetch.get("missing_ids") != []
        or fetch.get("unavailable_ids") != [memory_id]
    ):
        raise MemoryContractError("LOCAL_ONLY_FAILED")
    return {
        "index_count": 0,
        "fetch_count": 0,
        "unavailable_count": 1,
        "local_only_excluded": True,
    }


def _check_supersede(context: _Context) -> dict[str, Any]:
    if not context.shared_id:
        raise MemoryContractError("SUPERSEDE_FAILED")
    response = _request(
        context,
        "POST",
        "/v1/memory/actions",
        body={
            "actions": [
                {
                    "type": "memory.supersede",
                    "supersedes": context.shared_id,
                    "memory": {
                        "type": "fact",
                        "summary": _SUPERSEDE_SUMMARY,
                        "title": _SUPERSEDE_SUMMARY,
                        "content": _SUPERSEDE_CONTENT,
                        "description": _SUPERSEDE_CONTENT,
                        "source": "qa_memory_contract_supersede",
                    },
                    "reason": "Synthetic deterministic supersede qualification.",
                }
            ]
        },
        failure_code="SUPERSEDE_FAILED",
    )
    result = _result(response, failure_code="SUPERSEDE_FAILED")
    replacement_id = _memory_id(result, failure_code="SUPERSEDE_FAILED")
    if replacement_id == context.shared_id or result.get("action") != "memory.supersede":
        raise MemoryContractError("SUPERSEDE_FAILED")

    index = _request(
        context,
        "POST",
        "/v1/memory/index",
        body={"limit": 20},
        failure_code="SUPERSEDE_FAILED",
    )
    visible_ids = {str(item.get("id") or "") for item in _items(index)}
    default_fetch = _request(
        context,
        "POST",
        "/v1/memory/fetch",
        body={"ids": [context.shared_id, replacement_id]},
        failure_code="SUPERSEDE_FAILED",
    )
    replacement = [
        item for item in _items(default_fetch) if item.get("id") == replacement_id
    ]
    explicit_fetch = _request(
        context,
        "POST",
        "/v1/memory/fetch",
        body={
            "ids": [context.shared_id],
            "include_archived": True,
            "include_superseded": True,
        },
        failure_code="SUPERSEDE_FAILED",
    )
    explicit_old = [
        item for item in _items(explicit_fetch) if item.get("id") == context.shared_id
    ]
    old_raw = _get_raw(context, context.shared_id, failure_code="SUPERSEDE_FAILED")
    new_raw = _get_raw(context, replacement_id, failure_code="SUPERSEDE_FAILED")
    if (
        context.shared_id in visible_ids
        or replacement_id not in visible_ids
        or default_fetch.get("unavailable_ids") != [context.shared_id]
        or len(replacement) != 1
        or replacement[0].get("summary") != _SUPERSEDE_SUMMARY
        or replacement[0].get("content") != _SUPERSEDE_CONTENT
        or len(explicit_old) != 1
        or explicit_old[0].get("status") != "superseded"
        or old_raw.get("status") != "superseded"
        or old_raw.get("superseded_by") != replacement_id
        or context.shared_id not in (new_raw.get("supersedes") or [])
    ):
        raise MemoryContractError("SUPERSEDE_FAILED")
    return {
        "default_visible_count": 1,
        "explicit_visible_count": 1,
        "unavailable_count": 1,
        "superseded_hidden_by_default": True,
        "replacement_visible": True,
    }


def _legacy_envelope(context: _Context, *, source: str) -> dict[str, Any]:
    return _envelope(
        context,
        {
            "title": _LEGACY_TITLE,
            "description": _LEGACY_DESCRIPTION,
            "her_quote": "Synthetic legacy quote.",
            "linked_dimension": "QA contract",
        },
        visibility="shared",
        source=source,
        item_id=f"mom_qa_{secrets.token_hex(12)}",
    )


def _legacy_batch_item(
    context: _Context, memory_id: str, *, failure_code: str
) -> dict[str, Any]:
    batch_body = _request(
        context,
        "POST",
        "/v1/memory/legacy_batch",
        body={"batch_size": 50},
        failure_code=failure_code,
    )
    batch = batch_body.get("batch")
    matches = (
        [dict(item) for item in batch if isinstance(item, Mapping) and item.get("id") == memory_id]
        if isinstance(batch, list)
        else []
    )
    if len(matches) != 1 or not str(matches[0].get("old_body_hash") or ""):
        raise MemoryContractError(failure_code)
    return matches[0]


def _upgrade(
    context: _Context,
    memory_id: str,
    old_body_hash: str,
    *,
    summary: str,
    content: str,
    failure_code: str,
) -> dict[str, Any]:
    response = _request(
        context,
        "POST",
        "/v1/memory/actions",
        body={
            "actions": [
                {
                    "type": "memory.upgrade",
                    "id": memory_id,
                    "old_body_hash": old_body_hash,
                    "v1": {
                        "summary": summary,
                        "content": content,
                        "bucket": "QA contract",
                        "threads": ["memory qualification"],
                    },
                }
            ]
        },
        failure_code=failure_code,
    )
    result = _result(response, failure_code=failure_code)
    if result.get("skipped") == "migration_disabled":
        raise _MigrationUnavailable
    return result


def _check_legacy_migration(context: _Context) -> dict[str, Any]:
    memory_id = _add_envelope(
        context,
        _legacy_envelope(context, source="qa_memory_contract_legacy_stable_id"),
        failure_code="LEGACY_MIGRATION_FAILED",
    )
    batch_item = _legacy_batch_item(
        context, memory_id, failure_code="LEGACY_MIGRATION_FAILED"
    )
    result = _upgrade(
        context,
        memory_id,
        str(batch_item["old_body_hash"]),
        summary=_MIGRATED_SUMMARY,
        content=_MIGRATED_CONTENT,
        failure_code="LEGACY_MIGRATION_FAILED",
    )
    if result.get("status") != "ok" or _memory_id(
        result, failure_code="LEGACY_MIGRATION_FAILED"
    ) != memory_id:
        raise MemoryContractError("LEGACY_MIGRATION_FAILED")
    fetched = _request(
        context,
        "POST",
        "/v1/memory/fetch",
        body={"ids": [memory_id]},
        failure_code="LEGACY_MIGRATION_FAILED",
    )
    items = _items(fetched)
    if (
        len(items) != 1
        or items[0].get("id") != memory_id
        or items[0].get("summary") != _MIGRATED_SUMMARY
        or items[0].get("content") != _MIGRATED_CONTENT
    ):
        raise MemoryContractError("LEGACY_MIGRATION_FAILED")
    remaining = _request(
        context,
        "POST",
        "/v1/memory/legacy_batch",
        body={"batch_size": 50},
        failure_code="LEGACY_MIGRATION_FAILED",
    )
    if any(
        isinstance(item, Mapping) and item.get("id") == memory_id
        for item in (remaining.get("batch") or [])
    ):
        raise MemoryContractError("LEGACY_MIGRATION_FAILED")
    return {
        "fetch_count": 1,
        "stable_id_preserved": True,
        "legacy_shape_removed": True,
        "round_trip_verified": True,
    }


def _check_stale_cas(context: _Context) -> dict[str, Any]:
    memory_id = _add_envelope(
        context,
        _legacy_envelope(context, source="qa_memory_contract_stale_cas"),
        failure_code="STALE_CAS_FAILED",
    )
    batch_item = _legacy_batch_item(
        context, memory_id, failure_code="STALE_CAS_FAILED"
    )
    old_hash = str(batch_item["old_body_hash"])
    winner = _upgrade(
        context,
        memory_id,
        old_hash,
        summary=_CAS_WINNER_SUMMARY,
        content=_CAS_WINNER_CONTENT,
        failure_code="STALE_CAS_FAILED",
    )
    if winner.get("status") != "ok" or _memory_id(
        winner, failure_code="STALE_CAS_FAILED"
    ) != memory_id:
        raise MemoryContractError("STALE_CAS_FAILED")
    winner_raw = _get_raw(context, memory_id, failure_code="STALE_CAS_FAILED")

    sentinel_id = _add_envelope(
        context,
        _envelope(
            context,
            {
                "summary": _CAS_SENTINEL_SUMMARY,
                "content": _CAS_SENTINEL_CONTENT,
                "bucket": "QA contract",
                "threads": [],
            },
            visibility="shared",
            source="qa_memory_contract_concurrent_sentinel",
        ),
        failure_code="STALE_CAS_FAILED",
    )
    stale = _upgrade(
        context,
        memory_id,
        old_hash,
        summary=_CAS_STALE_SUMMARY,
        content=_CAS_STALE_CONTENT,
        failure_code="STALE_CAS_FAILED",
    )
    after_stale_raw = _get_raw(context, memory_id, failure_code="STALE_CAS_FAILED")
    fetched = _request(
        context,
        "POST",
        "/v1/memory/fetch",
        body={"ids": [memory_id, sentinel_id]},
        failure_code="STALE_CAS_FAILED",
    )
    by_id = {str(item.get("id") or ""): item for item in _items(fetched)}
    winner_item = by_id.get(memory_id) or {}
    sentinel_item = by_id.get(sentinel_id) or {}
    if (
        stale.get("status") != "ok"
        or stale.get("skipped") != "stale"
        or stale.get("noop") is not True
        or winner_raw.get("body_ct") != after_stale_raw.get("body_ct")
        or winner_item.get("summary") != _CAS_WINNER_SUMMARY
        or winner_item.get("content") != _CAS_WINNER_CONTENT
        or _contains_text(winner_item, _CAS_STALE_CONTENT)
        or sentinel_item.get("summary") != _CAS_SENTINEL_SUMMARY
        or sentinel_item.get("content") != _CAS_SENTINEL_CONTENT
    ):
        raise MemoryContractError("STALE_CAS_FAILED")
    return {
        "fetch_count": 2,
        "stable_id_preserved": True,
        "stale_write_rejected": True,
        "winning_update_preserved": True,
        "concurrent_card_preserved": True,
    }


def _not_run(check_id: str, layer: str) -> dict[str, Any]:
    return {
        "id": check_id,
        "layer": layer,
        "status": "NOT_RUN",
        "failure_code": "DEPENDENCY_NOT_RUN",
        "observations": {},
    }


def _receipt(profile_id: str, checks: list[dict[str, Any]], status: str, code: str) -> dict[str, Any]:
    counts = {
        label.lower(): sum(1 for check in checks if check.get("status") == label)
        for label in ("PASS", "FAIL", "NOT_EXERCISED", "NOT_RUN")
    }
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "kind": "memory_contract_smoke",
        "profile_id": profile_id,
        "target": "test",
        "account_lifecycle": "externally_managed",
        "status": status,
        "failure_code": code,
        "release_qualified": False,
        "checks": checks,
        "summary": counts,
        "sensitive_data_persisted": False,
        "raw_responses_persisted": False,
        "raw_memory_ids_persisted": False,
    }


def execute_memory_contract(
    client: _MemoryClient,
    session: Session,
    *,
    profile_id: str = EXPECTED_PROFILE_ID,
    base_url: str = DEFAULT_BASE_URL,
) -> dict[str, Any]:
    """Run the full contract on an externally managed, fresh synthetic account."""
    if profile_id != EXPECTED_PROFILE_ID:
        raise MemoryContractError("PROFILE_ID_INVALID")
    context = _Context(client=client, session=session, base_url=base_url)
    runners = (
        _check_fresh_empty,
        _check_encrypted_v1,
        _check_quiet_window_capture_write,
        _check_route_chat_message_trace,
        _check_capture_noop_disposable_chitchat,
        _check_duplicate_fact_no_growth,
        _check_local_only,
        _check_supersede,
        _check_legacy_migration,
        _check_stale_cas,
    )
    status = "PASS"
    failure_code = "NONE"
    for index, ((check_id, layer), runner) in enumerate(zip(_CHECKS, runners, strict=True)):
        try:
            observations = runner(context)
        except _MigrationUnavailable:
            context.checks.append(
                {
                    "id": check_id,
                    "layer": layer,
                    "status": "NOT_EXERCISED",
                    "failure_code": "MIGRATION_DISABLED",
                    "observations": {},
                }
            )
            for remaining_id, remaining_layer in _CHECKS[index + 1 :]:
                context.checks.append(
                    {
                        "id": remaining_id,
                        "layer": remaining_layer,
                        "status": "NOT_EXERCISED",
                        "failure_code": "MIGRATION_DISABLED",
                        "observations": {},
                    }
                )
            status = "UNVERIFIED"
            failure_code = "MIGRATION_DISABLED"
            break
        except MemoryContractError as exc:
            context.checks.append(
                {
                    "id": check_id,
                    "layer": layer,
                    "status": "FAIL",
                    "failure_code": exc.code,
                    "observations": {},
                }
            )
            context.checks.extend(
                _not_run(remaining_id, remaining_layer)
                for remaining_id, remaining_layer in _CHECKS[index + 1 :]
            )
            status = "FAIL"
            failure_code = exc.code
            break
        except Exception:
            context.checks.append(
                {
                    "id": check_id,
                    "layer": layer,
                    "status": "FAIL",
                    "failure_code": "INTERNAL_CHECK_ERROR",
                    "observations": {},
                }
            )
            context.checks.extend(
                _not_run(remaining_id, remaining_layer)
                for remaining_id, remaining_layer in _CHECKS[index + 1 :]
            )
            status = "FAIL"
            failure_code = "INTERNAL_CHECK_ERROR"
            break
        else:
            _record(context, check_id, layer, observations)
    return _receipt(profile_id, context.checks, status, failure_code)


def validate_receipt(receipt: Mapping[str, Any], schema_path: Path = DEFAULT_SCHEMA_PATH) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(dict(receipt))


def _validate_receipt_destination(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink() or path.exists():
        raise MemoryContractError("RECEIPT_PATH_UNSAFE")
    try:
        parent = path.parent.resolve(strict=True)
        metadata = parent.stat()
    except (OSError, RuntimeError):
        raise MemoryContractError("RECEIPT_PATH_UNSAFE") from None
    if (
        not parent.is_dir()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise MemoryContractError("RECEIPT_PATH_UNSAFE")


def _write_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    _validate_receipt_destination(path)
    payload = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise MemoryContractError("RECEIPT_WRITE_FAILED") from None


def run_smoke(
    manifest_path: Path,
    output_path: Path,
    *,
    expected_profile_id: str = EXPECTED_PROFILE_ID,
    client: _MemoryClient | None = None,
) -> dict[str, Any]:
    if expected_profile_id != EXPECTED_PROFILE_ID:
        raise MemoryContractError("PROFILE_ID_INVALID")
    _validate_receipt_destination(output_path)
    try:
        profile_id, base_url, session = load_profile_session(
            manifest_path, expected_profile_id
        )
    except Exception:
        raise MemoryContractError("SESSION_MANIFEST_INVALID") from None
    receipt = execute_memory_contract(
        client or SmokeClient(base_url),
        session,
        profile_id=profile_id,
        base_url=base_url,
    )
    try:
        validate_receipt(receipt)
    except Exception:
        raise MemoryContractError("RECEIPT_SCHEMA_INVALID") from None
    _write_receipt(output_path, receipt)
    return receipt


def exit_code(receipt: Mapping[str, Any]) -> int:
    if receipt.get("status") == "PASS":
        return 0
    if receipt.get("status") == "FAIL":
        return 1
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile-id", default=EXPECTED_PROFILE_ID)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = run_smoke(
            args.manifest,
            args.output,
            expected_profile_id=args.profile_id,
        )
    except MemoryContractError as exc:
        print(json.dumps({"status": "ERROR", "failure_code": exc.code}, separators=(",", ":")))
        return 2
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "failure_code": receipt["failure_code"],
                "release_qualified": False,
            },
            separators=(",", ":"),
        )
    )
    return exit_code(receipt)


if __name__ == "__main__":
    raise SystemExit(main())
