"""Genesis LLM client interface.

The caller supplies a runtime ProviderConfig whose api_key has been decrypted
inside the CVM/enclave path. This module never persists that key; only request
metadata, response text, and usage are cached for idempotency.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable

import db
import provider_client


@dataclass(frozen=True)
class GenesisLLMResult:
    text: str
    usage: dict
    cached: bool
    output_ref: str


CompletionFn = Callable[..., dict[str, Any]]
_user_semaphores: dict[str, threading.BoundedSemaphore] = {}
_user_semaphores_lock = threading.Lock()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def _per_user_concurrency() -> int:
    return max(1, min(_env_int("FEEDLING_GENESIS_LLM_USER_CONCURRENCY", 2), 16))


def _max_tokens_per_call() -> int:
    return max(128, min(_env_int("FEEDLING_GENESIS_LLM_MAX_TOKENS_PER_CALL", 4000), 32000))


@contextmanager
def _user_slot(user_id: str):
    with _user_semaphores_lock:
        sem = _user_semaphores.get(user_id)
        if sem is None:
            sem = threading.BoundedSemaphore(_per_user_concurrency())
            _user_semaphores[user_id] = sem
    acquired = sem.acquire(timeout=float(_env_int("FEEDLING_GENESIS_LLM_QUEUE_TIMEOUT_SEC", 30)))
    if not acquired:
        raise TimeoutError("genesis_llm_user_concurrency_timeout")
    try:
        yield
    finally:
        sem.release()


def _safe_output_type(idempotency_key: str) -> str:
    digest = hashlib.sha256(str(idempotency_key or "").encode("utf-8")).hexdigest()[:24]
    return f"llm:{digest}"


def _messages_hash(messages: list[dict[str, Any]]) -> str:
    raw = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class GenesisLLMClient:
    """Thin, idempotent wrapper around provider_client.chat_completion."""

    def __init__(self, completion_fn: CompletionFn | None = None):
        self._completion_fn = completion_fn or provider_client.chat_completion

    def complete(
        self,
        *,
        user_id: str,
        job_id: str,
        task_id: str,
        runtime: provider_client.ProviderConfig,
        messages: list[dict[str, Any]],
        max_tokens: int = 1200,
        timeout: float = 60.0,
        budget_label: str = "genesis",
        idempotency_key: str,
        temperature: float = 0.2,
        response_format: dict[str, Any] | None = None,
    ) -> GenesisLLMResult:
        if not idempotency_key:
            raise ValueError("idempotency_key_required")
        output_type = _safe_output_type(idempotency_key)
        cached = db.genesis_get_output(user_id, job_id, output_type)
        if cached and (cached.get("doc") or {}).get("text"):
            doc = cached["doc"]
            return GenesisLLMResult(
                text=str(doc.get("text") or ""),
                usage=doc.get("usage") if isinstance(doc.get("usage"), dict) else {},
                cached=True,
                output_ref=output_type,
            )

        capped_max_tokens = min(max_tokens, _max_tokens_per_call())
        with _user_slot(user_id):
            result = self._completion_fn(
                runtime,
                messages,
                max_tokens=capped_max_tokens,
                temperature=temperature,
                timeout=timeout,
                response_format=response_format,
            )
        text = str(result.get("reply") or "")
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        doc = {
            "task_id": task_id,
            "provider": runtime.provider,
            "model": runtime.model,
            "base_url": runtime.base_url,
            "messages_sha256": _messages_hash(messages),
            "max_tokens": capped_max_tokens,
            "timeout": timeout,
            "budget_label": budget_label,
            "text": text,
            "usage": usage,
        }
        db.genesis_upsert_output(user_id, job_id, output_type, doc=doc, status="done", ref=output_type)
        return GenesisLLMResult(text=text, usage=usage, cached=False, output_ref=output_type)
