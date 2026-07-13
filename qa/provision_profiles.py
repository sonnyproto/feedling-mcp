#!/usr/bin/env python3
"""Provision and clean up the private accounts used by API-key qualification.

This module is deliberately mechanical.  It is the credential boundary between
GitHub Actions secrets and the headless qualification agent: provider secrets
are consumed here and are never written to the private account manifest.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.provider_smoke.client import Session, SmokeClient  # noqa: E402


ALLOWED_BASE_URL = "https://test-api.feedling.app"
EXPECTED_RUNTIME_MODE = "db_action_v2"
EXPECTED_REASONING_EFFORT = "medium"
INVALID_PROVIDER_KEY = "feedling-e2e-intentionally-invalid"
MANIFEST_SCHEMA_VERSION = 1
SYNTHETIC_LABEL_PREFIX = "agent-e2e-"
SYNTHETIC_REAPER_PATH = "/v1/admin/qa/synthetic-account-reaper"
MAX_SYNTHETIC_TTL_SECONDS = 14_400
PROVISION_STATUS_READY = "ready"
PROVISION_STATUS_BLOCKED = "blocked"
PROVISION_FAILURE_NONE = "NONE"
PROVISION_FAILURE_INCOMPLETE = "PROVISIONING_INCOMPLETE"
OPERATIONAL_PROVISION_FAILURE_CODES = frozenset(
    {
        "FRESH_ACCOUNT_CHECK_FAILED",
        "REGISTRATION_VERIFICATION_FAILED",
        "ACCOUNT_NOT_FRESH",
        "INVALID_KEY_CHECK_FAILED",
        "INVALID_KEY_REJECTION_FAILED",
        "INVALID_KEY_ECHOED",
        "INVALID_KEY_ACCEPTED",
        "VALID_KEY_SETUP_FAILED",
        "VALID_KEY_REJECTED",
        "VALID_KEY_ECHOED",
        "VALID_KEY_ROUTE_MISMATCH",
        "TRACE_ENABLE_FAILED",
        "TRACE_UNAVAILABLE",
        "RUNTIME_MODE_SET_FAILED",
        "RUNTIME_MODE_VERIFICATION_FAILED",
    }
)


class ProvisionError(RuntimeError):
    """A sanitized provisioning failure safe to print in CI."""


class _ProfileProvisionFailure(RuntimeError):
    """A fixed-code operational failure isolated to one registered profile."""

    def __init__(self, code: str):
        if code not in OPERATIONAL_PROVISION_FAILURE_CODES:
            raise ValueError("unsupported profile provisioning failure code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ProfileSpec:
    provider: str
    route_family: str
    model_family: str
    credential_env: str
    model_env: str
    allowed_model_regex: str


PROFILE_SPECS: dict[str, ProfileSpec] = {
    "official-deepseek": ProfileSpec(
        provider="deepseek",
        route_family="official",
        model_family="deepseek",
        credential_env="QA_DEEPSEEK_API_KEY",
        model_env="QA_DEEPSEEK_MODEL",
        allowed_model_regex=r"^deepseek-[a-z0-9][a-z0-9._-]*$",
    ),
    "official-anthropic": ProfileSpec(
        provider="anthropic",
        route_family="official",
        model_family="claude",
        credential_env="QA_ANTHROPIC_API_KEY",
        model_env="QA_ANTHROPIC_MODEL",
        allowed_model_regex=r"^claude-[a-z0-9][a-z0-9._-]*$",
    ),
    "official-openai": ProfileSpec(
        provider="openai",
        route_family="official",
        model_family="openai",
        credential_env="QA_OPENAI_PROVIDER_API_KEY",
        model_env="QA_OPENAI_MODEL",
        allowed_model_regex=r"^(?:gpt-[a-z0-9][a-z0-9._-]*|o[1-9][a-z0-9._-]*)$",
    ),
    "openrouter-claude": ProfileSpec(
        provider="openrouter",
        route_family="openrouter",
        model_family="claude",
        credential_env="QA_OPENROUTER_API_KEY",
        model_env="QA_OPENROUTER_CLAUDE_MODEL",
        allowed_model_regex=r"^anthropic/claude-[a-z0-9][a-z0-9._:-]*$",
    ),
    "openrouter-openai": ProfileSpec(
        provider="openrouter",
        route_family="openrouter",
        model_family="openai",
        credential_env="QA_OPENROUTER_API_KEY",
        model_env="QA_OPENROUTER_OPENAI_MODEL",
        allowed_model_regex=r"^openai/(?:gpt-[a-z0-9][a-z0-9._:-]*|o[a-z0-9._:-]*)$",
    ),
    "openrouter-glm": ProfileSpec(
        provider="openrouter",
        route_family="openrouter",
        model_family="glm",
        credential_env="QA_OPENROUTER_API_KEY",
        model_env="QA_OPENROUTER_GLM_MODEL",
        allowed_model_regex=r"^(?:z-ai|thudm)/glm-[a-z0-9][a-z0-9._:-]*$",
    ),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required_env(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name) or "").strip()
    if not value:
        raise ProvisionError(f"missing required environment variable: {name}")
    return value


def _response_contains_secret(value: Any, secret: str) -> bool:
    if isinstance(value, str):
        return secret in value
    if isinstance(value, Mapping):
        return any(
            _response_contains_secret(key, secret)
            or _response_contains_secret(item, secret)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_response_contains_secret(item, secret) for item in value)
    return False


def validate_base_url(raw: str) -> str:
    """Return the one allowed test endpoint, rejecting redirect-like variants."""
    value = str(raw or "").strip()
    parsed = urllib.parse.urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ProvisionError(
            "QA_FEEDLING_BASE_URL is not the approved test endpoint"
        ) from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "test-api.feedling.app"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ProvisionError("QA_FEEDLING_BASE_URL is not the approved test endpoint")
    return ALLOWED_BASE_URL


def _load_coverage(path: Path) -> list[dict[str, Any]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ProvisionError(f"coverage lock not found: {path}") from None
    except (OSError, json.JSONDecodeError):
        raise ProvisionError(f"coverage lock is unreadable: {path}") from None

    profiles = doc.get("profiles") if isinstance(doc, dict) else None
    if not isinstance(profiles, list):
        raise ProvisionError("coverage lock must contain a profiles array")

    by_id: dict[str, dict[str, Any]] = {}
    for raw in profiles:
        if not isinstance(raw, dict):
            raise ProvisionError("coverage profile entries must be objects")
        profile_id = str(raw.get("profile_id") or raw.get("id") or "").strip()
        if not profile_id:
            raise ProvisionError("coverage profile is missing profile_id")
        if profile_id in by_id:
            raise ProvisionError(f"duplicate coverage profile: {profile_id}")
        normalized = dict(raw)
        normalized["profile_id"] = profile_id
        by_id[profile_id] = normalized

    expected = set(PROFILE_SPECS)
    actual = set(by_id)
    if actual != expected:
        missing = ",".join(sorted(expected - actual)) or "none"
        unexpected = ",".join(sorted(actual - expected)) or "none"
        raise ProvisionError(
            f"coverage profiles do not match the locked API-key matrix "
            f"(missing={missing}; unexpected={unexpected})"
        )

    ordered: list[dict[str, Any]] = []
    for profile_id in PROFILE_SPECS:
        profile = by_id[profile_id]
        spec = PROFILE_SPECS[profile_id]
        if str(profile.get("provider") or "").strip() != spec.provider:
            raise ProvisionError(
                f"provider mismatch for coverage profile: {profile_id}"
            )
        route_family = str(profile.get("route_family") or spec.route_family).strip()
        if route_family != spec.route_family:
            raise ProvisionError(
                f"route family mismatch for coverage profile: {profile_id}"
            )
        model_family = str(profile.get("model_family") or "").strip()
        if model_family != spec.model_family:
            raise ProvisionError(
                f"model family mismatch for coverage profile: {profile_id}"
            )
        credential_slot = str(
            profile.get("credential_slot")
            or profile.get("provider_key_env")
            or spec.credential_env
        ).strip()
        if credential_slot != spec.credential_env:
            raise ProvisionError(
                f"credential slot mismatch for coverage profile: {profile_id}"
            )
        model_env = str(profile.get("model_env") or "").strip()
        if model_env != spec.model_env:
            raise ProvisionError(
                f"model environment mismatch for profile: {profile_id}"
            )
        allowed_model_regex = profile.get("allowed_model_regex")
        if allowed_model_regex != spec.allowed_model_regex:
            raise ProvisionError(
                f"model constraint mismatch for coverage profile: {profile_id}"
            )
        ordered.append(profile)
    return ordered


def _model_for(
    profile: Mapping[str, Any], spec: ProfileSpec, env: Mapping[str, str]
) -> str:
    configured_env = str(profile.get("model_env") or spec.model_env).strip()
    if configured_env != spec.model_env:
        raise ProvisionError(
            f"model environment mismatch for profile: {profile['profile_id']}"
        )
    model = str(env.get(spec.model_env) or "").strip()
    if not model:
        raise ProvisionError(f"missing required model configuration: {spec.model_env}")
    if re.fullmatch(spec.allowed_model_regex, model) is None:
        raise ProvisionError(
            f"model configuration does not match the locked family for profile: "
            f"{profile['profile_id']}"
        )
    return model


def _reasoning_effort_for(profile: Mapping[str, Any]) -> str:
    profile_id = str(profile.get("profile_id") or "unknown")
    if profile.get("reasoning_expected") is not True:
        raise ProvisionError(
            f"reasoning must be required for coverage profile: {profile_id}"
        )
    effort = str(profile.get("reasoning_effort") or "").strip().lower()
    if effort != EXPECTED_REASONING_EFFORT:
        raise ProvisionError(
            f"reasoning effort must be {EXPECTED_REASONING_EFFORT} for coverage profile: {profile_id}"
        )
    return effort


def _atomic_write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(manifest, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Turn every redirect into a terminal HTTP response.

    ``urllib`` normally copies custom headers to a redirected request, including
    ``X-Admin-Token``.  Raising here prevents construction or dispatch of that
    second request, regardless of whether the target is same-origin.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "admin endpoint redirect rejected",
            headers,
            fp,
        )


class AdminClient:
    """Minimal admin transport kept separate from per-user SmokeClient auth."""

    def __init__(
        self, base_url: str, token: str, ssl_context: ssl.SSLContext | None = None
    ):
        self.base_url = validate_base_url(base_url)
        self._token = token
        self._ssl = ssl_context or ssl.create_default_context()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._ssl),
            _RejectRedirects(),
        )

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json", "X-Admin-Token": self._token},
            method=method,
        )
        for attempt in range(5):
            try:
                with self._opener.open(request, timeout=45) as response:
                    return response.status, json.loads(response.read() or b"{}")
            except urllib.error.HTTPError as exc:
                try:
                    payload = json.loads(exc.read() or b"{}")
                except Exception:
                    payload = {}
                return exc.code, payload
            except (
                urllib.error.URLError,
                TimeoutError,
                ssl.SSLError,
                ConnectionError,
                OSError,
            ):
                if attempt < 4:
                    time.sleep(2.0 * (attempt + 1))
        raise ProvisionError("admin endpoint was unreachable") from None


def _manifest_entry(
    profile: Mapping[str, Any],
    spec: ProfileSpec,
    model: str,
    reasoning_effort: str,
    session: Session,
    label: str,
) -> dict[str, Any]:
    return {
        "profile_id": profile["profile_id"],
        "label": label,
        "provider": spec.provider,
        "route_family": spec.route_family,
        "configured_model": model,
        "reasoning_effort": reasoning_effort,
        "user_id": session.user_id,
        "api_key": session.api_key,
        "secret_key_b64": base64.b64encode(session.sk).decode("ascii"),
        "public_key_b64": base64.b64encode(session.pk).decode("ascii"),
        "trace_enabled": False,
        "runtime_mode": "",
        "registration_verified": False,
        "fresh_state_verified": False,
        "invalid_key_rejected": False,
        "invalid_key_receipt": None,
        "valid_key_configured": False,
        "valid_key_receipt": None,
        "runtime_mode_set_verified": False,
        "runtime_mode_readback_verified": False,
        "provision_status": PROVISION_STATUS_BLOCKED,
        "provision_failure_code": PROVISION_FAILURE_INCOMPLETE,
    }


def _verify_synthetic_reaper(admin_client: AdminClient) -> dict[str, Any]:
    try:
        status, body = admin_client.request("GET", SYNTHETIC_REAPER_PATH)
    except Exception:
        raise ProvisionError("synthetic-account reaper preflight failed") from None
    if (
        status != 200
        or not isinstance(body, dict)
        or body.get("enabled") is not True
        or body.get("label_prefix") != SYNTHETIC_LABEL_PREFIX
        or not isinstance(body.get("max_ttl_seconds"), int)
        or isinstance(body.get("max_ttl_seconds"), bool)
        or not 1 <= body["max_ttl_seconds"] <= MAX_SYNTHETIC_TTL_SECONDS
    ):
        raise ProvisionError("synthetic-account reaper is not safely configured")
    return {
        "enabled": True,
        "label_prefix": SYNTHETIC_LABEL_PREFIX,
        "max_ttl_seconds": body["max_ttl_seconds"],
    }


def _check_fresh_account(
    client: SmokeClient, session: Session, entry: dict[str, Any]
) -> None:
    try:
        who_status, who_body = client._req(
            "GET", "/v1/users/whoami", api_key=session.api_key
        )
        chat_status, chat_body = client._req(
            "GET", "/v1/chat/history?limit=1", api_key=session.api_key
        )
        memory_status, memory_body = client._req(
            "GET", "/v1/memory/list?limit=1", api_key=session.api_key
        )
    except Exception:
        raise _ProfileProvisionFailure("FRESH_ACCOUNT_CHECK_FAILED") from None
    if (
        who_status != 200
        or not isinstance(who_body, Mapping)
        or who_body.get("user_id") != session.user_id
    ):
        raise _ProfileProvisionFailure("REGISTRATION_VERIFICATION_FAILED")
    entry["registration_verified"] = True
    if (
        chat_status != 200
        or memory_status != 200
        or not isinstance(chat_body, Mapping)
        or not isinstance(memory_body, Mapping)
        or (chat_body.get("messages") or [])
        or (memory_body.get("moments") or [])
    ):
        raise _ProfileProvisionFailure("ACCOUNT_NOT_FRESH")
    entry["fresh_state_verified"] = True


def _check_invalid_key(
    client: SmokeClient,
    session: Session,
    spec: ProfileSpec,
    model: str,
    reasoning_effort: str,
    entry: dict[str, Any],
) -> None:
    try:
        invalid_status, invalid_body = client.setup_raw(
            session,
            spec.provider,
            model,
            "",
            INVALID_PROVIDER_KEY,
            reasoning_effort=reasoning_effort,
        )
    except Exception:
        raise _ProfileProvisionFailure("INVALID_KEY_CHECK_FAILED") from None
    if not isinstance(invalid_body, Mapping):
        raise _ProfileProvisionFailure("INVALID_KEY_REJECTION_FAILED")
    if _response_contains_secret(invalid_body, INVALID_PROVIDER_KEY):
        raise _ProfileProvisionFailure("INVALID_KEY_ECHOED")
    provider_status = invalid_body.get("status_code")
    if (
        invalid_status != 400
        or invalid_body.get("error") != "provider_test_failed"
        or provider_status not in (400, 401, 403)
    ):
        if invalid_status == 200:
            raise _ProfileProvisionFailure("INVALID_KEY_ACCEPTED")
        raise _ProfileProvisionFailure("INVALID_KEY_REJECTION_FAILED")
    entry["invalid_key_rejected"] = True
    entry["invalid_key_receipt"] = {
        "http_status": invalid_status,
        "error": "provider_test_failed",
        "provider_status_code": provider_status,
    }


def _configure_valid_key(
    client: SmokeClient,
    session: Session,
    spec: ProfileSpec,
    model: str,
    reasoning_effort: str,
    provider_key: str,
    entry: dict[str, Any],
) -> None:
    try:
        valid_status, valid_body = client.setup_raw(
            session,
            spec.provider,
            model,
            "",
            provider_key,
            reasoning_effort=reasoning_effort,
        )
    except Exception:
        raise _ProfileProvisionFailure("VALID_KEY_SETUP_FAILED") from None
    if _response_contains_secret(valid_body, provider_key):
        raise _ProfileProvisionFailure("VALID_KEY_ECHOED")
    if isinstance(valid_body, Mapping) and (
        valid_status in (400, 401, 403)
        or valid_body.get("error") == "provider_test_failed"
    ):
        raise _ProfileProvisionFailure("VALID_KEY_REJECTED")
    configured = valid_body.get("config") if isinstance(valid_body, Mapping) else None
    if (
        valid_status != 200
        or not isinstance(valid_body, Mapping)
        or valid_body.get("status") != "configured"
        or not isinstance(configured, Mapping)
        or configured.get("provider") != spec.provider
        or configured.get("model") != model
        or configured.get("reasoning_effort") != reasoning_effort
    ):
        raise _ProfileProvisionFailure("VALID_KEY_ROUTE_MISMATCH")
    entry["valid_key_configured"] = True
    entry["valid_key_receipt"] = {
        "status": "configured",
        "provider": configured["provider"],
        "model": configured["model"],
        "reasoning_effort": configured["reasoning_effort"],
    }


def _enable_trace(client: SmokeClient, session: Session, entry: dict[str, Any]) -> None:
    try:
        trace_status, trace_body = client._req(
            "POST",
            "/v1/debug/trace/enable",
            api_key=session.api_key,
            body={"enabled": True},
        )
    except Exception:
        raise _ProfileProvisionFailure("TRACE_ENABLE_FAILED") from None
    if (
        trace_status != 200
        or not isinstance(trace_body, Mapping)
        or trace_body.get("enabled") is not True
        or trace_body.get("deploy_enabled") is not True
    ):
        raise _ProfileProvisionFailure("TRACE_UNAVAILABLE")
    entry["trace_enabled"] = True


def _set_runtime_mode(
    admin_client: AdminClient, session: Session, entry: dict[str, Any]
) -> None:
    try:
        set_status, set_body = admin_client.request(
            "POST",
            "/v1/admin/hosted-runtime-mode",
            {"user_id": session.user_id, "mode": EXPECTED_RUNTIME_MODE},
        )
    except Exception:
        raise _ProfileProvisionFailure("RUNTIME_MODE_SET_FAILED") from None
    if (
        set_status != 200
        or not isinstance(set_body, Mapping)
        or set_body.get("hosted_runtime_mode") != EXPECTED_RUNTIME_MODE
        or set_body.get("user_id") != session.user_id
    ):
        raise _ProfileProvisionFailure("RUNTIME_MODE_SET_FAILED")
    entry["runtime_mode_set_verified"] = True


def _verify_runtime_mode(
    admin_client: AdminClient, session: Session, entry: dict[str, Any]
) -> None:
    query = urllib.parse.urlencode({"user_id": session.user_id})
    try:
        get_status, get_body = admin_client.request(
            "GET", f"/v1/admin/hosted-runtime-mode?{query}"
        )
    except Exception:
        raise _ProfileProvisionFailure("RUNTIME_MODE_VERIFICATION_FAILED") from None
    if (
        get_status != 200
        or not isinstance(get_body, Mapping)
        or get_body.get("hosted_runtime_mode") != EXPECTED_RUNTIME_MODE
        or get_body.get("user_id") != session.user_id
    ):
        raise _ProfileProvisionFailure("RUNTIME_MODE_VERIFICATION_FAILED")
    entry["runtime_mode"] = EXPECTED_RUNTIME_MODE
    entry["runtime_mode_readback_verified"] = True


def _complete_diagnostic_manifest(manifest: Mapping[str, Any]) -> bool:
    profiles = manifest.get("profiles")
    if not isinstance(profiles, list):
        return False
    ids = [
        row.get("profile_id") if isinstance(row, Mapping) else None for row in profiles
    ]
    if ids != list(PROFILE_SPECS):
        return False
    for row in profiles:
        status = row.get("provision_status")
        failure_code = row.get("provision_failure_code")
        if status == PROVISION_STATUS_READY:
            if failure_code != PROVISION_FAILURE_NONE:
                return False
        elif status == PROVISION_STATUS_BLOCKED:
            if failure_code not in OPERATIONAL_PROVISION_FAILURE_CODES:
                return False
        else:
            return False
        if not all(
            isinstance(row.get(field), str) and bool(row.get(field))
            for field in ("user_id", "api_key", "secret_key_b64", "public_key_b64")
        ):
            return False
    return True


def _admin_confirms_user_absent(admin_client: AdminClient | None, user_id: str) -> bool:
    """Require the authenticated admin lookup's explicit not-found contract."""
    if admin_client is None or not user_id:
        return False
    path = f"/v1/admin/data-track/users/{urllib.parse.quote(user_id, safe='')}"
    try:
        status, body = admin_client.request("GET", path)
    except Exception:
        return False
    return status == 404 and body.get("error") == "user_not_found"


def _reset_one(
    client: SmokeClient,
    entry: Mapping[str, Any],
    admin_client: AdminClient | None = None,
) -> bool:
    api_key = str(entry.get("api_key") or "")
    user_id = str(entry.get("user_id") or "")
    if not api_key:
        return False
    try:
        status, body = client._req(
            "POST",
            "/v1/account/reset",
            api_key=api_key,
            body={"confirm": "delete-all-data"},
        )
        if status == 200 and body.get("deleted") is True:
            return True
        # A retry after a successful-but-lost reset response returns 401 because
        # the key is revoked.  A wrong key also returns 401, so it is safe to
        # accept only after the admin lookup independently proves the synthetic
        # user no longer exists.
        if status == 401:
            return _admin_confirms_user_absent(admin_client, user_id)
        return False
    except Exception:
        return False


def cleanup(
    manifest_path: Path,
    *,
    env: Mapping[str, str] | None = None,
    client: SmokeClient | None = None,
    admin_client: AdminClient | None = None,
) -> dict[str, Any]:
    """Reset every account in a private manifest, deleting it only on success."""
    if not manifest_path.exists():
        return {
            "attempted": 0,
            "cleaned": 0,
            "failed_profile_ids": [],
            "manifest_deleted": False,
            "manifest_missing": True,
        }
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ProvisionError("private manifest is unreadable") from None
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ProvisionError("private manifest schema version is unsupported")
    base_url = validate_base_url(str(manifest.get("base_url") or ""))
    entries = manifest.get("profiles")
    if not isinstance(entries, list):
        raise ProvisionError("private manifest has no profiles array")
    active_client = client or SmokeClient(base_url)
    active_env = os.environ if env is None else env
    verification_admin = admin_client
    if verification_admin is None:
        admin_token = str(active_env.get("QA_TEST_ADMIN_TOKEN") or "").strip()
        if admin_token:
            verification_admin = AdminClient(
                base_url,
                admin_token,
                getattr(active_client, "_ssl", None),
            )

    cleaned = 0
    failed: list[str] = []
    seen_users: set[str] = set()
    for raw in entries:
        entry = raw if isinstance(raw, dict) else {}
        profile_id = str(entry.get("profile_id") or "unknown")
        user_id = str(entry.get("user_id") or "")
        if user_id and user_id in seen_users:
            continue
        if user_id:
            seen_users.add(user_id)
        if _reset_one(active_client, entry, verification_admin):
            cleaned += 1
        else:
            failed.append(profile_id)

    deleted = False
    if not failed:
        manifest_path.unlink(missing_ok=True)
        deleted = True
    return {
        "attempted": len(seen_users) if seen_users else len(entries),
        "cleaned": cleaned,
        "failed_profile_ids": failed,
        "manifest_deleted": deleted,
        "manifest_missing": False,
    }


def provision(
    coverage_path: Path,
    manifest_path: Path,
    *,
    env: Mapping[str, str] | None = None,
    client: SmokeClient | None = None,
    admin_client: AdminClient | None = None,
) -> dict[str, Any]:
    """Create the locked matrix, isolating operational failures by profile."""
    active_env = os.environ if env is None else env
    base_url = validate_base_url(_required_env(active_env, "QA_FEEDLING_BASE_URL"))
    admin_token = _required_env(active_env, "QA_TEST_ADMIN_TOKEN")
    profiles = _load_coverage(coverage_path)
    # Validate every static input before the reaper preflight or registration.
    # A missing credential is a broken run contract; an expired credential is a
    # per-profile diagnostic discovered later by the valid-key probe.
    models = {
        str(profile["profile_id"]): _model_for(
            profile, PROFILE_SPECS[str(profile["profile_id"])], active_env
        )
        for profile in profiles
    }
    provider_keys = {
        profile_id: _required_env(active_env, spec.credential_env)
        for profile_id, spec in PROFILE_SPECS.items()
    }
    reasoning_efforts = {
        str(profile["profile_id"]): _reasoning_effort_for(profile)
        for profile in profiles
    }
    active_client = client or SmokeClient(base_url)
    active_admin = admin_client or AdminClient(
        base_url, admin_token, active_client._ssl
    )
    reaper_receipt = _verify_synthetic_reaper(active_admin)
    run_id = re.sub(
        r"[^A-Za-z0-9_.-]+", "-", str(active_env.get("QA_RUN_ID") or "local")
    )[:48]
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "base_url": base_url,
        "runtime_mode": EXPECTED_RUNTIME_MODE,
        "synthetic_account_reaper": reaper_receipt,
        "profiles": [],
    }

    try:
        for profile in profiles:
            profile_id = str(profile["profile_id"])
            spec = PROFILE_SPECS[profile_id]
            provider_key = provider_keys[profile_id]
            model = models[profile_id]
            reasoning_effort = reasoning_efforts[profile_id]
            label = f"{SYNTHETIC_LABEL_PREFIX}{run_id}-{profile_id}"

            try:
                session = active_client.register(label)
            except Exception:
                raise ProvisionError(
                    f"account registration failed for profile: {profile_id}"
                ) from None
            try:
                entry = _manifest_entry(
                    profile, spec, model, reasoning_effort, session, label
                )
            except Exception:
                _reset_one(
                    active_client,
                    {
                        "profile_id": profile_id,
                        "user_id": str(getattr(session, "user_id", "")),
                        "api_key": str(getattr(session, "api_key", "")),
                    },
                    active_admin,
                )
                raise ProvisionError(
                    f"account registration failed for profile: {profile_id}"
                ) from None
            manifest["profiles"].append(entry)
            _atomic_write_manifest(manifest_path, manifest)

            try:
                _check_fresh_account(active_client, session, entry)
                _atomic_write_manifest(manifest_path, manifest)
                _check_invalid_key(
                    active_client, session, spec, model, reasoning_effort, entry
                )
                _atomic_write_manifest(manifest_path, manifest)
                _configure_valid_key(
                    active_client,
                    session,
                    spec,
                    model,
                    reasoning_effort,
                    provider_key,
                    entry,
                )
                _atomic_write_manifest(manifest_path, manifest)
                _enable_trace(active_client, session, entry)
                _atomic_write_manifest(manifest_path, manifest)
                _set_runtime_mode(active_admin, session, entry)
                _atomic_write_manifest(manifest_path, manifest)
                _verify_runtime_mode(active_admin, session, entry)
                entry["provision_status"] = PROVISION_STATUS_READY
                entry["provision_failure_code"] = PROVISION_FAILURE_NONE
                _atomic_write_manifest(manifest_path, manifest)
            except _ProfileProvisionFailure as failure:
                entry["provision_status"] = PROVISION_STATUS_BLOCKED
                entry["provision_failure_code"] = failure.code
                _atomic_write_manifest(manifest_path, manifest)
                continue
        if not _complete_diagnostic_manifest(manifest):
            raise ProvisionError(
                "provisioning did not produce a complete diagnostic manifest"
            )
    except Exception:
        # Best effort prevents failed setup attempts from accumulating accounts.
        # If any reset fails, cleanup deliberately leaves the 0600 manifest so
        # the workflow's `if: always()` cleanup step can retry.
        try:
            result = cleanup(
                manifest_path,
                env=active_env,
                client=active_client,
                admin_client=active_admin,
            )
            if result["manifest_missing"]:
                for entry in manifest["profiles"]:
                    _reset_one(active_client, entry, active_admin)
        except Exception:
            # The in-memory checkpoint remains sufficient to attempt cleanup
            # even if the on-disk manifest itself became unreadable.
            for entry in manifest["profiles"]:
                _reset_one(active_client, entry, active_admin)
        raise

    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("provision", help="create the locked API-key profiles")
    create.add_argument("--coverage", type=Path, default=Path("qa/coverage-lock.json"))
    create.add_argument("--manifest", type=Path, required=True)
    remove = commands.add_parser(
        "cleanup", help="reset all accounts in a private manifest"
    )
    remove.add_argument("--manifest", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "provision":
            result = provision(args.coverage, args.manifest)
            if not _complete_diagnostic_manifest(result):
                raise ProvisionError(
                    "provisioning did not produce a complete diagnostic manifest"
                )
            ready = [
                row["profile_id"]
                for row in result["profiles"]
                if row["provision_status"] == PROVISION_STATUS_READY
            ]
            blocked = [
                row["profile_id"]
                for row in result["profiles"]
                if row["provision_status"] == PROVISION_STATUS_BLOCKED
            ]
            print(
                json.dumps(
                    {
                        "ok": True,
                        "profile_count": len(result["profiles"]),
                        "ready_profile_count": len(ready),
                        "blocked_profile_count": len(blocked),
                        "blocked_profile_ids": blocked,
                        "manifest": str(args.manifest),
                    }
                )
            )
            return 0
        result = cleanup(args.manifest)
        print(
            json.dumps(
                {
                    "ok": not result["failed_profile_ids"],
                    "attempted": result["attempted"],
                    "cleaned": result["cleaned"],
                    "failed_profile_ids": result["failed_profile_ids"],
                    "manifest_deleted": result["manifest_deleted"],
                }
            )
        )
        return 0 if not result["failed_profile_ids"] else 1
    except ProvisionError as exc:
        print(f"provisioning error: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("provisioning error: internal failure", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
