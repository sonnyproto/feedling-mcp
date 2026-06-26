"""Small dependency-free helpers shared across domains."""

import json
import os
import re
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_ENV_TRUTHY = {"1", "true", "yes", "y", "on"}


def _env_flag_enabled(name: str, default: str = "false") -> bool:
    return str(os.environ.get(name, default) or "").strip().lower() in _ENV_TRUTHY


RUNTIME_V2_DEFAULT_ON_ENV = "FEEDLING_RUNTIME_V2_DEFAULT_ON"


def runtime_v2_default_on() -> bool:
    """Baseline default for the perception/resident V2 rollout flags.

    OFF by default so prod keeps the dormant legacy path. Set
    ``FEEDLING_RUNTIME_V2_DEFAULT_ON=true`` in the test enclave so test users run
    V2 without a per-user blob flip. An explicit per-user blob value still
    overrides this baseline (operator opt-in/opt-out wins).
    """
    return _env_flag_enabled(RUNTIME_V2_DEFAULT_ON_ENV)


# io-onboarding docs branch this code serves skill_url from. MUST match the
# feedling-mcp deploy branch:
#   test branch (deploys test-api) -> "test"; main branch (deploys api) -> "main".
# ⚠️ When merging test->main, flip this to "main" — it is the ONLY line to change;
#    every skill_url is derived from it via io_onboarding_skill_url().
IO_ONBOARDING_BRANCH = "test"
_IO_ONBOARDING_RAW_BASE = (
    f"https://raw.githubusercontent.com/teleport-computer/io-onboarding/{IO_ONBOARDING_BRANCH}"
)


def io_onboarding_skill_url(filename: str) -> str:
    """Raw URL for an io-onboarding skill doc on the branch matching this deploy."""
    return f"{_IO_ONBOARDING_RAW_BASE}/{filename}"


def _now_iso() -> str:
    return datetime.now().isoformat()


def _safe_zoneinfo(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _new_public_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _strip_json_code_fence(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _json_from_model_text(text: str):
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty model response")
    try:
        return json.loads(raw)
    except Exception:
        pass
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(raw):
        if ch not in "{[":
            continue
        try:
            obj, _ = decoder.raw_decode(raw[idx:])
            return obj
        except Exception:
            continue
    raise ValueError("no json object found")


def _to_epoch(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return 0.0
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0


def _epoch_to_iso(epoch: float) -> str:
    try:
        if epoch and epoch > 0:
            return datetime.fromtimestamp(float(epoch)).isoformat()
    except Exception:
        pass
    return ""
