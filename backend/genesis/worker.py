"""CVM-side Genesis worker.

This module is intended to run inside the agent-runner service. It claims
finalized import jobs, decrypts chunk envelopes via the enclave, calls the
user-configured LLM provider with the user's key, and posts only distilled
reducer output back to the backend apply route.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from typing import Any, Callable

import httpx

import db
import provider_client
from core.store import get_store
from genesis import prompts, service
from genesis.llm_client import GenesisLLMClient

GENESIS_WORKER_SCOPES = ["envelope_decrypt", "genesis"]
AI_PERSONA_SOURCE_KINDS = {
    "agent_prompt",
    "ai_persona",
    "character",
    "character_card",
    "companion_persona",
    # voice/persona backfill for pre-genesis host users: material is assembled from
    # the existing identity record (tone_style/custom_persona_prompt/self_introduction),
    # not an uploaded transcript — but it must route to the ai_persona family so the
    # worker runs persona_build (cutover gate 4 A).
    "companion_persona_backfill",
    "system_prompt",
}
MEMORY_SUMMARY_SOURCE_KINDS = {
    "memory_summary",
    "memories_summary",
    "memory_digest",
}
USER_PROFILE_SOURCE_KINDS = {
    "personal_profile",
    "persona",
    "persona_profile",
    "profile",
    "user_persona",
    "user_profile",
}


class GenesisWorkerError(Exception):
    """Retryable/non-retryable worker failure surfaced into job.error."""


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def _headers(runtime_token: str) -> dict[str, str]:
    return {"X-Feedling-Runtime-Token": runtime_token}


def _mint(mint_runtime_token: Callable, user_id: str) -> str:
    try:
        token = mint_runtime_token(user_id, scopes=GENESIS_WORKER_SCOPES)
    except TypeError:
        token = mint_runtime_token(user_id, GENESIS_WORKER_SCOPES)
    token = str(token or "").strip()
    if not token:
        raise GenesisWorkerError("runtime_token_unavailable")
    return token


def _json_object(text: str, *, task_id: str) -> dict:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw).strip()
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start:end + 1]
    try:
        parsed = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        raise GenesisWorkerError(f"{task_id}:invalid_json") from e
    if not isinstance(parsed, dict):
        raise GenesisWorkerError(f"{task_id}:json_not_object")
    return parsed


def _chunks(seq: list[Any], size: int) -> list[list[Any]]:
    safe = max(1, size)
    return [seq[idx:idx + safe] for idx in range(0, len(seq), safe)]


def _source_family(source_kind: str) -> str:
    kind = re.sub(r"[^a-z0-9_]+", "_", str(source_kind or "").strip().lower()).strip("_")
    if kind.endswith("_import"):
        kind = kind[:-7]
    if kind in AI_PERSONA_SOURCE_KINDS:
        return "ai_persona"
    if kind in MEMORY_SUMMARY_SOURCE_KINDS:
        return "memory_summary"
    if kind in USER_PROFILE_SOURCE_KINDS:
        return "user_profile"
    return "history"


def _joined_material(chunk_texts: list[str]) -> str:
    parts = []
    for idx, text in enumerate(chunk_texts):
        cleaned = str(text or "").strip()
        if cleaned:
            parts.append(f"--- chunk {idx + 1} ---\n{cleaned}")
    return "\n\n".join(parts).strip()


def _source_tagged_fact_text(source_family: str, text: str) -> str:
    if source_family == "user_profile":
        return (
            "source_kind=user_profile\n"
            "This material is the user's own profile/persona. Extract only durable facts about the user. "
            "Do not infer the companion's name, identity, personality, dimensions, or voice from it.\n\n"
            f"{text}"
        )
    return text


def _strip_identity(doc: dict) -> dict:
    clean = dict(doc)
    clean.pop("identity", None)
    clean.pop("days_with_user", None)
    clean.pop("relationship_anchor_evidence", None)
    return clean


def _identity_only(doc: dict) -> dict:
    identity = doc.get("identity") if isinstance(doc.get("identity"), dict) else {}
    dims = identity.get("dimensions") if isinstance(identity.get("dimensions"), list) else []
    out = {"memories": []}
    if identity.get("agent_name") or dims:
        out["identity"] = identity
    if out.get("identity") and doc.get("days_with_user") is not None:
        out["days_with_user"] = doc.get("days_with_user")
    if out.get("identity") and doc.get("relationship_anchor_evidence"):
        out["relationship_anchor_evidence"] = doc.get("relationship_anchor_evidence")
    return out


def _fetch_provider_key(api_url: str, enclave_url: str, runtime_token: str) -> str:
    try:
        resp = httpx.get(
            f"{api_url.rstrip('/')}/v1/model_api/key_envelope",
            headers=_headers(runtime_token),
            timeout=15,
        )
        resp.raise_for_status()
        envelope = resp.json().get("api_key_envelope")
    except Exception as e:  # noqa: BLE001
        raise GenesisWorkerError(f"provider_key_envelope_fetch_failed:{type(e).__name__}") from e
    if not isinstance(envelope, dict):
        raise GenesisWorkerError("provider_key_envelope_missing")
    return _decrypt_envelope(
        enclave_url,
        runtime_token,
        envelope,
        purpose="model_api_provider_key",
    ).decode("utf-8")


def _runtime_for_user(user_id: str, provider_key: str) -> provider_client.ProviderConfig:
    config = db.get_blob(user_id, "model_api")
    if not isinstance(config, dict):
        raise GenesisWorkerError("model_api_not_configured")
    if config.get("test_status") != "ok":
        raise GenesisWorkerError(f"model_api_not_tested:{config.get('test_status', '')}")
    try:
        provider, model, base_url = provider_client.validate_config(
            str(config.get("provider") or ""),
            str(config.get("model") or ""),
            str(config.get("base_url") or ""),
        )
    except provider_client.ProviderError as e:
        raise GenesisWorkerError(f"model_api_config_invalid:{str(e)[:120]}") from e
    return provider_client.ProviderConfig(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=provider_key,
    )


def _decrypt_envelope(enclave_url: str, runtime_token: str, envelope: dict, *, purpose: str) -> bytes:
    try:
        resp = httpx.post(
            f"{enclave_url.rstrip('/')}/v1/envelope/decrypt",
            headers=_headers(runtime_token),
            json={"envelope": envelope, "purpose": purpose},
            timeout=30,
            verify=False,
        )
        resp.raise_for_status()
        body = resp.json()
        plaintext_b64 = body.get("plaintext_b64") if isinstance(body, dict) else ""
        return base64.b64decode(str(plaintext_b64 or ""), validate=True)
    except Exception as e:  # noqa: BLE001
        raise GenesisWorkerError(f"{purpose}:decrypt_failed:{type(e).__name__}") from e


def _decrypt_chunks(enclave_url: str, runtime_token: str, chunks: list[dict]) -> list[str]:
    texts: list[str] = []
    for chunk in chunks:
        envelope = service.chunk_envelope_from_row(chunk)
        raw = _decrypt_envelope(enclave_url, runtime_token, envelope, purpose="genesis_chunk")
        texts.append(raw.decode("utf-8"))
    return texts


def _decrypt_blob_text(enclave_url: str, runtime_token: str, blob: dict, *, purpose: str) -> str:
    envelope = blob.get("content_envelope") if isinstance(blob.get("content_envelope"), dict) else {}
    if not envelope:
        return ""
    return _decrypt_envelope(enclave_url, runtime_token, envelope, purpose=purpose).decode("utf-8")


def _existing_persona_material(user_id: str, enclave_url: str, runtime_token: str) -> dict:
    try:
        blob = db.get_blob(user_id, service.GENESIS_PERSONA_BLOB)
        if not isinstance(blob, dict):
            return {}
        try:
            priority = int(blob.get("source_priority") or 0)
        except Exception:
            priority = 0
        if priority < 100:
            return {}
        content = _decrypt_blob_text(enclave_url, runtime_token, blob, purpose="genesis_persona").strip()
        if not content:
            return {}
        return {
            "content": content,
            "source_family": str(blob.get("source_family") or ""),
            "source_priority": priority,
        }
    except Exception:
        return {}


def _existing_voice_workset(user_id: str, enclave_url: str, runtime_token: str) -> dict:
    try:
        blob = db.get_blob(user_id, service.GENESIS_VOICE_BLOB)
        if not isinstance(blob, dict):
            return {}
        raw = _decrypt_blob_text(enclave_url, runtime_token, blob, purpose="genesis_voice")
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _complete_json(
    llm: GenesisLLMClient,
    *,
    user_id: str,
    job_id: str,
    task_id: str,
    runtime: provider_client.ProviderConfig,
    messages: list[dict[str, str]],
    max_tokens: int,
    idempotency_key: str,
    temperature: float = 0.2,
) -> dict:
    result = llm.complete(
        user_id=user_id,
        job_id=job_id,
        task_id=task_id,
        runtime=runtime,
        messages=messages,
        max_tokens=max_tokens,
        timeout=float(_env_int("FEEDLING_GENESIS_LLM_TIMEOUT_SEC", 90)),
        idempotency_key=idempotency_key,
        temperature=temperature,
    )
    return _json_object(result.text, task_id=task_id)


def _complete_text(
    llm: GenesisLLMClient,
    *,
    user_id: str,
    job_id: str,
    task_id: str,
    runtime: provider_client.ProviderConfig,
    messages: list[dict[str, str]],
    max_tokens: int,
    idempotency_key: str,
    temperature: float = 0.2,
) -> str:
    result = llm.complete(
        user_id=user_id,
        job_id=job_id,
        task_id=task_id,
        runtime=runtime,
        messages=messages,
        max_tokens=max_tokens,
        timeout=float(_env_int("FEEDLING_GENESIS_LLM_TIMEOUT_SEC", 90)),
        idempotency_key=idempotency_key,
        temperature=temperature,
    )
    return result.text


def _voice_reduce(
    llm: GenesisLLMClient,
    *,
    user_id: str,
    job_id: str,
    runtime: provider_client.ProviderConfig,
    candidates: list[dict],
) -> dict:
    if not candidates:
        return {"behavior_notes": [], "exemplars": []}
    batch_size = max(2, _env_int("FEEDLING_GENESIS_VOICE_REDUCE_BATCH", 24))
    current = list(candidates)
    round_no = 0
    while len(current) > batch_size:
        next_round: list[dict] = []
        for idx, batch in enumerate(_chunks(current, batch_size)):
            reduced = _complete_json(
                llm,
                user_id=user_id,
                job_id=job_id,
                task_id=f"voice-reduce-{round_no}-{idx}",
                runtime=runtime,
                messages=prompts.voice_reduce_messages(batch),
                max_tokens=2200,
                idempotency_key=f"{job_id}:voice_reduce:{round_no}:{idx}",
            )
            next_round.append({
                "behavior_notes_candidates": reduced.get("behavior_notes") or [],
                "exemplar_candidates": reduced.get("exemplars") or [],
            })
        current = next_round
        round_no += 1
    return _complete_json(
        llm,
        user_id=user_id,
        job_id=job_id,
        task_id=f"voice-reduce-{round_no}",
        runtime=runtime,
        messages=prompts.voice_reduce_messages(current),
        max_tokens=2600,
        idempotency_key=f"{job_id}:voice_reduce:{round_no}:final",
    )


def _fact_write(
    llm: GenesisLLMClient,
    *,
    user_id: str,
    job_id: str,
    runtime: provider_client.ProviderConfig,
    fact_candidates: list[dict],
    persona_material: str = "",
    memory_summary: str = "",
) -> dict:
    if not fact_candidates and not persona_material and not memory_summary:
        return {"memories": [], "identity": {"agent_name": "", "dimensions": []}}
    batch_size = max(4, _env_int("FEEDLING_GENESIS_FACT_WRITE_BATCH", 80))
    outputs: list[dict] = []
    for idx, batch in enumerate(_chunks(fact_candidates, batch_size) or [[]]):
        outputs.append(_complete_json(
            llm,
            user_id=user_id,
            job_id=job_id,
            task_id=f"fact-write-{idx}",
            runtime=runtime,
            messages=prompts.fact_write_messages(batch, persona_material, memory_summary),
            max_tokens=3000,
            idempotency_key=f"{job_id}:fact_write:{idx}",
        ))
    memories: list[dict] = []
    dims: list[dict] = []
    agent_name = ""
    days_with_user = 0
    evidence: list[str] = []
    for output in outputs:
        if isinstance(output.get("memories"), list):
            memories.extend(item for item in output["memories"] if isinstance(item, dict))
        identity = output.get("identity") if isinstance(output.get("identity"), dict) else {}
        if not agent_name and identity.get("agent_name"):
            agent_name = str(identity.get("agent_name") or "")
        if isinstance(identity.get("dimensions"), list):
            dims.extend(item for item in identity["dimensions"] if isinstance(item, dict))
        try:
            days_with_user = max(days_with_user, int(output.get("days_with_user") or 0))
        except Exception:
            pass
        if output.get("relationship_anchor_evidence"):
            evidence.append(str(output.get("relationship_anchor_evidence") or ""))
    return {
        "memories": memories,
        "identity": {"agent_name": agent_name, "dimensions": dims[:7]},
        "days_with_user": days_with_user,
        "relationship_anchor_evidence": " | ".join(evidence)[:500],
    }


def _build_reducer_output(
    *,
    user_id: str,
    job_id: str,
    runtime: provider_client.ProviderConfig,
    chunk_texts: list[str],
    source_kind: str = "history",
    existing_persona: dict | None = None,
    existing_voice: dict | None = None,
) -> dict:
    llm = GenesisLLMClient()
    source_family = _source_family(source_kind)
    material = _joined_material(chunk_texts)
    existing_persona = existing_persona if isinstance(existing_persona, dict) else {}
    existing_voice = existing_voice if isinstance(existing_voice, dict) else {}

    if source_family == "ai_persona":
        existing_notes = existing_voice.get("behavior_notes") if isinstance(existing_voice.get("behavior_notes"), list) else []
        existing_exemplars = existing_voice.get("exemplars") if isinstance(existing_voice.get("exemplars"), list) else []
        founding = [item for item in existing_exemplars if isinstance(item, dict) and item.get("founding")]
        if not founding:
            founding = [item for item in existing_exemplars if isinstance(item, dict)][:12]
        persona_source_family = "merged" if existing_notes or founding else "ai_persona"
        identity_doc = _identity_only(_fact_write(
            llm,
            user_id=user_id,
            job_id=job_id,
            runtime=runtime,
            fact_candidates=[],
            persona_material=material,
        ))
        persona_content = _complete_text(
            llm,
            user_id=user_id,
            job_id=job_id,
            task_id="persona-build",
            runtime=runtime,
            messages=prompts.persona_build_messages(material, existing_notes, founding),
            max_tokens=2600,
            idempotency_key=f"{job_id}:persona_build",
        )
        return {
            **identity_doc,
            "source_kind": source_kind,
            "source_family": source_family,
            "persona": {
                "content": persona_content,
                "prompt_version": "7.B",
                "source_kind": source_kind,
                "source_family": persona_source_family,
            },
            "voice": {
                "behavior_notes_count": len(existing_notes),
                "exemplar_count": len(existing_exemplars),
                "founding_exemplar_count": len(founding),
            },
        }

    if source_family == "memory_summary":
        fact_write = _strip_identity(_fact_write(
            llm,
            user_id=user_id,
            job_id=job_id,
            runtime=runtime,
            fact_candidates=[],
            memory_summary=material,
        ))
        return {
            **fact_write,
            "source_kind": source_kind,
            "source_family": source_family,
            "voice": {
                "behavior_notes_count": 0,
                "exemplar_count": 0,
                "founding_exemplar_count": 0,
            },
        }

    voice_candidates: list[dict] = []
    fact_candidates: list[dict] = []
    for idx, text in enumerate(chunk_texts):
        if source_family == "history":
            voice = _complete_json(
                llm,
                user_id=user_id,
                job_id=job_id,
                task_id=f"voice-map-{idx}",
                runtime=runtime,
                messages=prompts.voice_map_messages(text),
                max_tokens=1800,
                idempotency_key=f"{job_id}:voice_map:{idx}",
            )
            voice_candidates.append(voice)
        facts = _complete_json(
            llm,
            user_id=user_id,
            job_id=job_id,
            task_id=f"fact-map-{idx}",
            runtime=runtime,
            messages=prompts.fact_map_messages(_source_tagged_fact_text(source_family, text)),
            max_tokens=1800,
            idempotency_key=f"{job_id}:fact_map:{idx}",
        )
        if isinstance(facts.get("fact_candidates"), list):
            fact_candidates.extend(item for item in facts["fact_candidates"] if isinstance(item, dict))

    voice_final = _voice_reduce(
        llm,
        user_id=user_id,
        job_id=job_id,
        runtime=runtime,
        candidates=voice_candidates,
    ) if source_family == "history" else {"behavior_notes": [], "exemplars": []}
    exemplars = voice_final.get("exemplars") if isinstance(voice_final.get("exemplars"), list) else []
    founding = [item for item in exemplars if isinstance(item, dict) and item.get("founding")]
    if not founding:
        founding = [item for item in exemplars if isinstance(item, dict)][:12]
    behavior_notes = voice_final.get("behavior_notes") if isinstance(voice_final.get("behavior_notes"), list) else []
    voice_workset = {
        "behavior_notes": behavior_notes,
        "exemplars": exemplars,
    }

    fact_write = _fact_write(
        llm,
        user_id=user_id,
        job_id=job_id,
        runtime=runtime,
        fact_candidates=fact_candidates,
    )
    if source_family == "user_profile":
        fact_write = _strip_identity(fact_write)
        return {
            **fact_write,
            "source_kind": source_kind,
            "source_family": source_family,
            "voice": {
                "behavior_notes_count": 0,
                "exemplar_count": 0,
                "founding_exemplar_count": 0,
            },
        }

    persona_material = str(existing_persona.get("content") or "").strip()
    persona_source_family = "merged" if persona_material else "history"
    persona_content = _complete_text(
        llm,
        user_id=user_id,
        job_id=job_id,
        task_id="persona-build",
        runtime=runtime,
        messages=prompts.persona_build_messages(persona_material, behavior_notes, founding),
        max_tokens=2600,
        idempotency_key=f"{job_id}:persona_build",
    )
    return {
        **fact_write,
        "source_kind": source_kind,
        "source_family": source_family,
        "persona": {
            "content": persona_content,
            "prompt_version": "7.B",
            "source_kind": source_kind,
            "source_family": persona_source_family,
        },
        "voice": {
            "behavior_notes_count": len(behavior_notes),
            "exemplar_count": len(exemplars),
            "founding_exemplar_count": len(founding),
        },
        "voice_workset": voice_workset,
    }


def _apply_reducer_output(api_url: str, runtime_token: str, job_id: str, output: dict) -> dict:
    try:
        resp = httpx.post(
            f"{api_url.rstrip('/')}/v1/genesis/imports/{job_id}/outputs",
            headers=_headers(runtime_token),
            json={"reducer_output": output},
            timeout=60,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:  # noqa: BLE001
        raise GenesisWorkerError(f"apply_outputs_failed:{type(e).__name__}") from e
    return body if isinstance(body, dict) else {}


def _process_job(job: dict, *, api_url: str, enclave_url: str, mint_runtime_token: Callable) -> dict:
    user_id = str(job.get("user_id") or "")
    job_id = str(job.get("job_id") or "")
    if not user_id or not job_id:
        raise GenesisWorkerError("invalid_claimed_job")
    store = get_store(user_id)
    service.write_genesis_state(store, {**job, "status": "processing"}, status="processing")
    total_chunks = int(job.get("total_chunks") or 0)
    if total_chunks <= 0:
        raise GenesisWorkerError("empty_import")
    missing = db.genesis_missing_chunk_seqs(user_id, job_id, total_chunks)
    if missing:
        raise GenesisWorkerError(f"missing_chunks:{len(missing)}")
    chunks = db.genesis_list_chunks(user_id, job_id)
    if total_chunks and len(chunks) != total_chunks:
        raise GenesisWorkerError(f"chunk_count_mismatch:{len(chunks)}:{total_chunks}")

    token = _mint(mint_runtime_token, user_id)
    provider_key = _fetch_provider_key(api_url, enclave_url, token)
    runtime = _runtime_for_user(user_id, provider_key)
    chunk_texts = _decrypt_chunks(enclave_url, token, chunks)
    existing_persona = _existing_persona_material(user_id, enclave_url, token)
    existing_voice = _existing_voice_workset(user_id, enclave_url, token)
    reducer_output = _build_reducer_output(
        user_id=user_id,
        job_id=job_id,
        runtime=runtime,
        chunk_texts=chunk_texts,
        source_kind=str(job.get("source_kind") or "history"),
        existing_persona=existing_persona,
        existing_voice=existing_voice,
    )
    applied = _apply_reducer_output(api_url, token, job_id, reducer_output)
    return {
        "user_id": user_id,
        "job_id": job_id,
        "status": "done",
        "chunks": len(chunks),
        "applied": applied.get("applied", applied),
    }


def tick(
    *,
    api_url: str,
    enclave_url: str,
    mint_runtime_token: Callable,
    max_jobs: int = 1,
    now=None,
) -> dict:
    """Process up to max_jobs uploaded genesis jobs once.

    ``mint_runtime_token(user_id, scopes=...)`` must return a short-lived token
    carrying ``envelope_decrypt`` and ``genesis`` scopes. ``now`` is accepted for
    supervisor/test symmetry; this implementation only reports elapsed time.
    """
    start = time.time() if now is None else float(now())
    jobs = db.genesis_claim_uploaded_jobs(limit=max_jobs)
    results: list[dict] = []
    failed = 0
    for job in jobs:
        user_id = str(job.get("user_id") or "")
        job_id = str(job.get("job_id") or "")
        try:
            results.append(_process_job(
                job,
                api_url=api_url,
                enclave_url=enclave_url,
                mint_runtime_token=mint_runtime_token,
            ))
        except Exception as e:  # noqa: BLE001
            failed += 1
            if user_id and job_id:
                store = get_store(user_id)
                service.mark_failed(store, job_id, f"worker_failed:{type(e).__name__}:{str(e)[:180]}")
            results.append({"user_id": user_id, "job_id": job_id, "status": "failed", "error": str(e)[:240]})
    end = time.time() if now is None else float(now())
    return {
        "claimed": len(jobs),
        "processed": len([item for item in results if item.get("status") == "done"]),
        "failed": failed,
        "results": results,
        "elapsed_ms": max(0, int((end - start) * 1000)),
    }
