"""Shared readside memory core for HTTP routes and hosted agent tools."""

from __future__ import annotations

import os
from typing import Any, Callable

import httpx

from memory import service as memory_service


MEMORY_READSIDE_DEFAULT_LIMIT = 50
MEMORY_READSIDE_DEFAULT_HARD_MAX = 1000
# Backward-compatible default value. Do not use this as the effective cap for
# recall windows; use effective_readside_limit() so env overrides and the
# "0 = full window up to HARD_MAX" sentinel are respected.
MEMORY_READSIDE_LIMIT = MEMORY_READSIDE_DEFAULT_LIMIT
MEMORY_FETCH_TOOL_LIMIT = 5
MEMORY_FETCH_LOOP_LIMIT = 8

_SALIENCE_WEIGHT = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}
_INACTIVE_STATUSES = {
    "archived",
    "deleted",
    "superseded",
}

PostEnclave = Callable[[str | None, list[dict], ...], dict]


def _status(moment: dict) -> str:
    return str(moment.get("status") or "active").strip().lower() or "active"


def _salience(moment: dict) -> str:
    salience = str(moment.get("salience") or "medium").strip().lower()
    return salience if salience in _SALIENCE_WEIGHT else "medium"


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _time_key(moment: dict) -> str:
    for key in ("last_active", "updated_at", "occurred_at", "created_at"):
        value = str(moment.get(key) or "").strip()
        if value:
            return value
    return ""


def memory_available(
    moment: dict,
    owner_user_id: str,
    *,
    include_archived: bool = False,
    include_superseded: bool = False,
) -> bool:
    if not isinstance(moment, dict):
        return False
    if moment.get("owner_user_id") != owner_user_id:
        return False
    if moment.get("visibility") == "local_only":
        return False
    if not moment.get("K_enclave"):
        return False
    status = _status(moment)
    if status == "superseded" and not include_superseded:
        return False
    if status in {"archived", "deleted"} and not include_archived:
        return False
    if status in _INACTIVE_STATUSES and status not in {"archived", "superseded"}:
        return False
    if memory_service._memory_is_archived(moment) and not include_archived:
        return False
    return True


def memory_score(moment: dict) -> float:
    salience_score = _SALIENCE_WEIGHT.get(_salience(moment), 2)
    importance = _float(moment.get("importance"), 0.5)
    open_bonus = 1.0 if moment.get("is_open_thread") is True else 0.0
    return round(open_bonus + salience_score + importance, 4)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def readside_hard_max() -> int:
    hard_max = _env_int("FEEDLING_MEMORY_READSIDE_HARD_MAX", MEMORY_READSIDE_DEFAULT_HARD_MAX)
    return max(1, hard_max)


def configured_readside_limit() -> int:
    limit = _env_int("FEEDLING_MEMORY_READSIDE_LIMIT", MEMORY_READSIDE_DEFAULT_LIMIT)
    return limit if limit >= 0 else MEMORY_READSIDE_DEFAULT_LIMIT


def effective_readside_limit(value: Any | None = None) -> int:
    """Return the effective recall candidate window.

    Config knobs:
    - FEEDLING_MEMORY_READSIDE_LIMIT defaults to 50.
    - FEEDLING_MEMORY_READSIDE_LIMIT=0 means "full recall window".
    - FEEDLING_MEMORY_READSIDE_HARD_MAX is the safety valve; even full-window
      mode will not decrypt/return more than this many candidate envelopes.

    Important: full-window mode only disables top-N truncation. Eligibility
    filtering, sorting, and selector behavior must stay enabled.
    """
    if value is None or str(value).strip() == "":
        requested = configured_readside_limit()
    else:
        try:
            requested = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid limit") from exc
        if requested < 0:
            raise ValueError("invalid limit")
    hard_max = readside_hard_max()
    if requested == 0:
        return hard_max
    return max(1, min(requested, hard_max))


def readside_candidates(
    moments: list,
    owner_user_id: str,
    *,
    limit: int | None = None,
) -> tuple[list[dict], int]:
    candidates = [
        dict(moment, score=memory_score(moment))
        for moment in moments
        if memory_available(moment, owner_user_id)
    ]
    candidates.sort(
        key=lambda m: (
            1 if m.get("is_open_thread") is True else 0,
            _SALIENCE_WEIGHT.get(_salience(m), 2),
            _float(m.get("importance"), 0.5),
            _time_key(m),
            str(m.get("id") or ""),
        ),
        reverse=True,
    )
    capped_limit = effective_readside_limit(limit)
    return candidates[:capped_limit], len(candidates)


def post_enclave_readside(
    api_key: str | None,
    candidates: list[dict],
    *,
    operation: str,
    payload: dict | None = None,
) -> dict:
    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        raise RuntimeError("enclave_unavailable")
    if not api_key:
        raise RuntimeError("api_key_unavailable")
    body = dict(payload or {})
    body["moments"] = candidates
    try:
        with httpx.Client(timeout=20, verify=False) as client:
            resp = client.post(
                f"{enclave_url}/v1/memory/{operation}",
                headers={"X-API-Key": api_key},
                json=body,
            )
    except httpx.HTTPError as e:
        raise RuntimeError(f"enclave_error:{type(e).__name__}") from e
    if resp.status_code >= 400:
        raise RuntimeError(f"enclave_http_{resp.status_code}:{resp.text[:180]}")
    response = resp.json()
    if not isinstance(response, dict):
        raise RuntimeError("enclave_invalid_readside_response")
    return response


def memory_index_core(
    store,
    api_key: str | None,
    payload: dict | None = None,
    *,
    post_enclave: Callable[..., dict] | None = None,
) -> dict:
    payload = dict(payload or {})
    limit = effective_readside_limit(payload.get("limit"))
    candidates, user_card_count = readside_candidates(
        memory_service._load_moments(store),
        store.user_id,
        limit=limit,
    )
    response = (post_enclave or post_enclave_readside)(
        api_key,
        candidates,
        operation="index",
        payload={
            "include_sensitive": bool(payload.get("include_sensitive", False)),
            "limit": limit,
            "query": str(payload.get("query") or "")[:500],
        },
    )
    items = response.get("items") if isinstance(response.get("items"), list) else []
    return {
        "items": items,
        "limit": limit,
        "truncated": user_card_count > len(candidates),
        "user_card_count": user_card_count,
    }


def _bool_payload(value: Any) -> bool:
    return str(value or "").lower() in {"1", "true", "yes", "on"}


def memory_fetch_core(
    store,
    api_key: str | None,
    payload: dict | None = None,
    *,
    post_enclave: Callable[..., dict] | None = None,
) -> dict:
    payload = dict(payload or {})
    ids = payload.get("ids")
    if not isinstance(ids, list) or any(not isinstance(mid, str) or not mid.strip() for mid in ids):
        raise ValueError("ids must be a list of non-empty strings")
    limit = effective_readside_limit(payload.get("limit"))
    ids = [mid.strip() for mid in ids[:limit]]
    include_archived = _bool_payload(payload.get("include_archived"))
    include_superseded = _bool_payload(payload.get("include_superseded"))
    by_id = {m.get("id"): m for m in memory_service._load_moments(store) if isinstance(m, dict)}
    missing_ids: list[str] = []
    unavailable_ids: list[str] = []
    candidates: list[dict] = []
    for memory_id in ids:
        moment = by_id.get(memory_id)
        if not isinstance(moment, dict) or moment.get("owner_user_id") != store.user_id:
            missing_ids.append(memory_id)
            continue
        if not memory_available(
            moment,
            store.user_id,
            include_archived=include_archived,
            include_superseded=include_superseded,
        ):
            unavailable_ids.append(memory_id)
            continue
        candidates.append(moment)
    response = (post_enclave or post_enclave_readside)(
        api_key,
        candidates,
        operation="fetch",
        payload={"ids": [m.get("id") for m in candidates], "limit": limit},
    )
    enclave_unavailable = response.get("unavailable_ids") if isinstance(response.get("unavailable_ids"), list) else []
    unavailable_ids.extend(str(mid) for mid in enclave_unavailable if isinstance(mid, str))
    items_by_id = {
        item.get("id"): item
        for item in (response.get("items") if isinstance(response.get("items"), list) else [])
        if isinstance(item, dict)
    }
    return {
        "items": [items_by_id[mid] for mid in ids if mid in items_by_id],
        "missing_ids": missing_ids,
        "unavailable_ids": unavailable_ids,
    }
