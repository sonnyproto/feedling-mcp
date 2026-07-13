from __future__ import annotations

import io
import json
import stat
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from qa import provision_profiles as provisioner
from tools.provider_smoke.client import Session, SmokeError


VALID_MODELS = {
    "official-deepseek": "deepseek-v4-flash",
    "official-anthropic": "claude-sonnet-4-5",
    "official-openai": "gpt-5.4",
    "official-gemini": "gemini-2.5-flash",
    "openrouter-claude": "anthropic/claude-sonnet-4.5",
    "openrouter-openai": "openai/gpt-4.1-mini",
    "openrouter-glm": "z-ai/glm-4.5-air:free",
    "relay-kongbeiqie": "[特价纯血]claude-opus-4-6",
}


PROFILE_ROWS = [
    {
        "profile_id": profile_id,
        "provider": spec.provider,
        "route_family": spec.route_family,
        "model_family": spec.model_family,
        "credential_slot": spec.credential_env,
        "model_env": spec.model_env,
        "allowed_model_regex": spec.allowed_model_regex,
        **(
            {
                "base_url_env": spec.base_url_env,
                "allowed_base_url": spec.allowed_base_url,
            }
            if spec.base_url_env
            else {}
        ),
        "reasoning_expected": True,
        "reasoning_effort": provisioner.EXPECTED_REASONING_EFFORT,
    }
    for profile_id, spec in provisioner.PROFILE_SPECS.items()
]


def _write_coverage(tmp_path: Path, rows=None) -> Path:
    path = tmp_path / "coverage-lock.json"
    path.write_text(json.dumps({"profiles": PROFILE_ROWS if rows is None else rows}))
    return path


def _env() -> dict[str, str]:
    env = {
        "QA_FEEDLING_BASE_URL": provisioner.ALLOWED_BASE_URL,
        "QA_TEST_ADMIN_TOKEN": "admin-sensitive-value",
        "QA_DEEPSEEK_API_KEY": "deepseek-sensitive-value",
        "QA_ANTHROPIC_API_KEY": "anthropic-sensitive-value",
        "QA_OPENAI_PROVIDER_API_KEY": "openai-sensitive-value",
        "QA_GEMINI_API_KEY": "gemini-sensitive-value",
        "QA_OPENROUTER_API_KEY": "openrouter-sensitive-value",
        "QA_KONGBEIQIE_API_KEY": "kongbeiqie-sensitive-value",
        "QA_KONGBEIQIE_BASE_URL": provisioner.ALLOWED_KONGBEIQIE_BASE_URL,
        "QA_RUN_ID": "unit/42",
    }
    for profile_id, spec in provisioner.PROFILE_SPECS.items():
        env[spec.model_env] = VALID_MODELS[profile_id]
    return env


class FakeSmokeClient:
    def __init__(self):
        self.registered: list[tuple[str, Session]] = []
        self.setup_calls: list[tuple[str, str, str, str, str, str | None]] = []
        self.trace_calls: list[str] = []
        self.reset_calls: list[tuple[str, dict]] = []
        self.accept_invalid = False
        self.echo_invalid_secret = False
        self.echo_valid_secret = False
        self.invalid_http_status = 400
        self.invalid_provider_status = 401
        self.fail_valid_for: str | None = None
        self.reject_valid_for: str | None = None
        self.fail_registration_at: int | None = None
        self.trace_deploy_enabled = True
        self.reset_fail_for: set[str] = set()
        self.already_reset_for: set[str] = set()

    def register(self, label: str) -> Session:
        index = len(self.registered)
        if self.fail_registration_at == index:
            raise SmokeError("register", "synthetic registration failure")
        session = Session(
            user_id=f"user-{index}",
            api_key=f"feedling-account-key-{index}",
            sk=bytes([index + 1]) * 32,
            pk=bytes([index + 11]) * 32,
        )
        self.registered.append((label, session))
        return session

    def setup(
        self, session, provider, model, base_url, api_key, *, reasoning_effort=None
    ):
        self.setup_calls.append(
            (session.user_id, provider, model, base_url, api_key, reasoning_effort)
        )
        if self.fail_valid_for == session.user_id:
            raise SmokeError("setup", f"provider echoed secret={api_key}")
        return {
            "provider": provider,
            "model": model,
            "base_url": self._configured_base_url(provider, base_url),
            "reasoning_effort": reasoning_effort,
        }

    @staticmethod
    def _configured_base_url(provider: str, requested_base_url: str) -> str:
        if requested_base_url:
            return requested_base_url.rstrip("/")
        return {
            "deepseek": "https://api.deepseek.com",
            "anthropic": "https://api.anthropic.com/v1",
            "openai": "https://api.openai.com/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta",
            "openrouter": "https://openrouter.ai/api/v1",
        }[provider]

    def setup_raw(
        self, session, provider, model, base_url, api_key, *, reasoning_effort=None
    ):
        self.setup_calls.append(
            (session.user_id, provider, model, base_url, api_key, reasoning_effort)
        )
        if api_key != provisioner.INVALID_PROVIDER_KEY:
            if self.fail_valid_for == session.user_id:
                raise SmokeError("setup", f"provider echoed secret={api_key}")
            if self.reject_valid_for == session.user_id:
                return 400, {
                    "error": "provider_test_failed",
                    "detail": "provider authentication rejected",
                    "status_code": 401,
                }
            config = {
                "provider": provider,
                "model": model,
                "base_url": self._configured_base_url(provider, base_url),
                "reasoning_effort": reasoning_effort,
            }
            if self.echo_valid_secret:
                config["nested"] = {"credential": api_key}
            return 200, {"status": "configured", "config": config}
        if self.accept_invalid:
            return 200, {
                "status": "configured",
                "config": {"provider": provider, "model": model},
            }
        body = {
            "error": "provider_test_failed",
            "detail": "provider authentication rejected",
            "status_code": self.invalid_provider_status,
        }
        if self.echo_invalid_secret:
            body["nested"] = {"detail": f"rejected secret={api_key}"}
        return self.invalid_http_status, body

    def _req(self, method, path, *, api_key=None, body=None, **_kwargs):
        if path == "/v1/users/whoami":
            session = next(s for _label, s in self.registered if s.api_key == api_key)
            return 200, {"user_id": session.user_id, "active_route": "model_api"}
        if path == "/v1/chat/history?limit=1":
            return 200, {"messages": []}
        if path == "/v1/memory/list?limit=1":
            return 200, {"moments": []}
        if path == "/v1/debug/trace/enable":
            self.trace_calls.append(api_key)
            assert method == "POST"
            assert body == {"enabled": True}
            return 200, {"enabled": True, "deploy_enabled": self.trace_deploy_enabled}
        if path == "/v1/account/reset":
            self.reset_calls.append((api_key, body))
            if api_key in self.already_reset_for:
                return 401, {"error": "unauthorized"}
            if api_key in self.reset_fail_for:
                return 503, {"error": "unavailable"}
            return 200, {"deleted": True}
        raise AssertionError(f"unexpected request: {method} {path}")


class FakeAdminClient:
    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.modes: dict[str, str] = {}
        self.missing_users: set[str] = set()
        self.user_lookup_status: int | None = None

    def request(self, method: str, path: str, body=None):
        self.calls.append((method, path, body))
        if path == provisioner.SYNTHETIC_REAPER_PATH:
            return 200, {
                "enabled": True,
                "label_prefix": provisioner.SYNTHETIC_LABEL_PREFIX,
                "max_ttl_seconds": provisioner.MAX_SYNTHETIC_TTL_SECONDS,
            }
        if path.startswith("/v1/admin/data-track/users/"):
            user_id = path.rsplit("/", 1)[1]
            if self.user_lookup_status is not None:
                return self.user_lookup_status, {"error": "lookup_unavailable"}
            if user_id in self.missing_users:
                return 404, {"error": "user_not_found"}
            return 200, {"user": {"user_id": user_id}}
        if method == "POST":
            self.modes[body["user_id"]] = body["mode"]
            return 200, {
                "user_id": body["user_id"],
                "hosted_runtime_mode": body["mode"],
            }
        user_id = path.split("user_id=", 1)[1]
        return 200, {
            "user_id": user_id,
            "hosted_runtime_mode": self.modes[user_id],
        }


def test_provision_creates_all_profiles_without_persisting_provider_secrets(tmp_path):
    coverage = _write_coverage(tmp_path)
    manifest_path = tmp_path / "private" / "profiles.json"
    env = _env()
    smoke = FakeSmokeClient()
    admin = FakeAdminClient()

    result = provisioner.provision(
        coverage, manifest_path, env=env, client=smoke, admin_client=admin
    )

    assert len(result["profiles"]) == len(provisioner.PROFILE_SPECS) == 8
    assert len(smoke.registered) == 8
    assert len(smoke.setup_calls) == 16
    assert len(smoke.trace_calls) == 8
    assert len(admin.calls) == 17
    assert admin.calls[0] == ("GET", provisioner.SYNTHETIC_REAPER_PATH, None)
    for index in range(0, len(smoke.setup_calls), 2):
        assert smoke.setup_calls[index][4] == provisioner.INVALID_PROVIDER_KEY
        assert smoke.setup_calls[index + 1][4] != provisioner.INVALID_PROVIDER_KEY
        assert smoke.setup_calls[index][5] == provisioner.EXPECTED_REASONING_EFFORT
        assert smoke.setup_calls[index + 1][5] == provisioner.EXPECTED_REASONING_EFFORT
    for call in smoke.setup_calls:
        profile_id = next(
            row["profile_id"] for row in result["profiles"] if row["user_id"] == call[0]
        )
        expected_request_base_url = (
            provisioner.ALLOWED_KONGBEIQIE_BASE_URL
            if profile_id == "relay-kongbeiqie"
            else ""
        )
        assert call[3] == expected_request_base_url
    assert all(row["invalid_key_rejected"] for row in result["profiles"])
    assert all(row["valid_key_configured"] for row in result["profiles"])
    assert all(row["registration_verified"] for row in result["profiles"])
    assert all(row["fresh_state_verified"] for row in result["profiles"])
    assert all(row["trace_enabled"] for row in result["profiles"])
    assert all(row["runtime_mode"] == "db_action_v2" for row in result["profiles"])
    assert all(row["runtime_mode_set_verified"] for row in result["profiles"])
    assert all(row["runtime_mode_readback_verified"] for row in result["profiles"])
    assert [row["profile_id"] for row in result["profiles"]] == list(
        provisioner.PROFILE_SPECS
    )
    assert all(
        row["provision_status"] == provisioner.PROVISION_STATUS_READY
        and row["provision_failure_code"] == provisioner.PROVISION_FAILURE_NONE
        for row in result["profiles"]
    )
    assert all(
        row["reasoning_effort"] == provisioner.EXPECTED_REASONING_EFFORT
        for row in result["profiles"]
    )
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600

    raw = manifest_path.read_text()
    for name, value in env.items():
        if name.endswith("API_KEY") or name == "QA_TEST_ADMIN_TOKEN":
            assert value not in raw
    persisted = json.loads(raw)
    assert {p["profile_id"] for p in persisted["profiles"]} == set(
        provisioner.PROFILE_SPECS
    )
    for profile in persisted["profiles"]:
        spec = provisioner.PROFILE_SPECS[profile["profile_id"]]
        assert profile["configured_model"] == VALID_MODELS[profile["profile_id"]]
        assert profile["configured_base_url"] == spec.expected_configured_base_url
        assert profile["invalid_key_receipt"] == {
            "http_status": 400,
            "error": "provider_test_failed",
            "provider_status_code": 401,
        }
        assert profile["valid_key_receipt"] == {
            "status": "configured",
            "provider": profile["provider"],
            "model": profile["configured_model"],
            "base_url": spec.expected_configured_base_url,
            "reasoning_effort": provisioner.EXPECTED_REASONING_EFFORT,
        }


def test_invalid_and_valid_setup_calls_use_profile_locked_base_urls(tmp_path):
    smoke = FakeSmokeClient()
    result = provisioner.provision(
        _write_coverage(tmp_path),
        tmp_path / "manifest.json",
        env=_env(),
        client=smoke,
        admin_client=FakeAdminClient(),
    )
    profile_by_user = {row["user_id"]: row for row in result["profiles"]}

    for index in range(0, len(smoke.setup_calls), 2):
        invalid_call = smoke.setup_calls[index]
        valid_call = smoke.setup_calls[index + 1]
        assert invalid_call[0] == valid_call[0]
        profile = profile_by_user[invalid_call[0]]
        expected_request_base_url = (
            provisioner.ALLOWED_KONGBEIQIE_BASE_URL
            if profile["profile_id"] == "relay-kongbeiqie"
            else ""
        )
        assert invalid_call[3] == expected_request_base_url
        assert valid_call[3] == expected_request_base_url
        assert profile["configured_base_url"] == (
            provisioner.PROFILE_SPECS[
                profile["profile_id"]
            ].expected_configured_base_url
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://test-api.feedling.app",
        "https://test-api.feedling.app.evil.example",
        "https://test-api.feedling.app/collect",
        "https://test-api.feedling.app:not-a-port",
        "https://user@test-api.feedling.app",
        "https://test-api.feedling.app?next=https://evil.example",
    ],
)
def test_base_url_allowlist_rejects_variants(url):
    with pytest.raises(provisioner.ProvisionError, match="approved test endpoint"):
        provisioner.validate_base_url(url)


def test_admin_redirect_handler_rejects_without_constructing_forward_request():
    token = "admin-sensitive-value"
    request = urllib.request.Request(
        provisioner.ALLOWED_BASE_URL + "/v1/admin/data-track/users/u1",
        headers={"X-Admin-Token": token},
    )
    handler = provisioner._RejectRedirects()

    with pytest.raises(urllib.error.HTTPError) as caught:
        handler.redirect_request(
            request,
            io.BytesIO(b""),
            302,
            "Found",
            {"Location": "https://attacker.example/collect"},
            "https://attacker.example/collect",
        )

    assert caught.value.code == 302
    assert caught.value.url == request.full_url
    assert token not in str(caught.value)


def test_admin_client_installs_reject_redirect_handler():
    client = provisioner.AdminClient(
        provisioner.ALLOWED_BASE_URL,
        "admin-sensitive-value",
    )
    assert any(
        isinstance(handler, provisioner._RejectRedirects)
        for handler in client._opener.handlers
    )


def test_coverage_must_contain_exact_locked_profiles(tmp_path):
    coverage = _write_coverage(tmp_path, PROFILE_ROWS[:-1])
    with pytest.raises(
        provisioner.ProvisionError, match="coverage profiles do not match"
    ):
        provisioner.provision(
            coverage,
            tmp_path / "manifest.json",
            env=_env(),
            client=FakeSmokeClient(),
            admin_client=FakeAdminClient(),
        )


def test_provisioning_refuses_to_register_without_safe_server_reaper(tmp_path):
    admin = FakeAdminClient()

    def unsafe_reaper(method, path, body=None):
        admin.calls.append((method, path, body))
        return 200, {
            "enabled": False,
            "label_prefix": provisioner.SYNTHETIC_LABEL_PREFIX,
            "max_ttl_seconds": provisioner.MAX_SYNTHETIC_TTL_SECONDS,
        }

    admin.request = unsafe_reaper
    smoke = FakeSmokeClient()
    with pytest.raises(
        provisioner.ProvisionError, match="reaper is not safely configured"
    ):
        provisioner.provision(
            _write_coverage(tmp_path),
            tmp_path / "manifest.json",
            env=_env(),
            client=smoke,
            admin_client=admin,
        )

    assert smoke.registered == []


def test_all_static_credentials_are_validated_before_reaper_or_registration(tmp_path):
    env = _env()
    del env["QA_ANTHROPIC_API_KEY"]
    smoke = FakeSmokeClient()
    admin = FakeAdminClient()

    with pytest.raises(
        provisioner.ProvisionError,
        match="missing required environment variable: QA_ANTHROPIC_API_KEY",
    ):
        provisioner.provision(
            _write_coverage(tmp_path),
            tmp_path / "manifest.json",
            env=env,
            client=smoke,
            admin_client=admin,
        )

    assert smoke.registered == []
    assert admin.calls == []


def test_missing_relay_base_url_fails_before_external_state(tmp_path):
    env = _env()
    del env["QA_KONGBEIQIE_BASE_URL"]
    smoke = FakeSmokeClient()
    admin = FakeAdminClient()

    with pytest.raises(
        provisioner.ProvisionError,
        match="missing required environment variable: QA_KONGBEIQIE_BASE_URL",
    ):
        provisioner.provision(
            _write_coverage(tmp_path),
            tmp_path / "manifest.json",
            env=env,
            client=smoke,
            admin_client=admin,
        )

    assert smoke.registered == []
    assert admin.calls == []


@pytest.mark.parametrize(
    "unapproved_url",
    [
        "http://xn--vduyey89e.com/v1",
        "https://relay.example/v1",
        f"{provisioner.ALLOWED_KONGBEIQIE_BASE_URL}/",
        "https://user@xn--vduyey89e.com/v1",
        "https://xn--vduyey89e.com/v1?forward=https://attacker.example",
    ],
)
def test_unapproved_relay_base_url_is_rejected_without_echo_or_external_state(
    tmp_path, unapproved_url
):
    env = _env()
    env["QA_KONGBEIQIE_BASE_URL"] = unapproved_url
    smoke = FakeSmokeClient()
    admin = FakeAdminClient()

    with pytest.raises(provisioner.ProvisionError, match="locked endpoint") as caught:
        provisioner.provision(
            _write_coverage(tmp_path),
            tmp_path / "manifest.json",
            env=env,
            client=smoke,
            admin_client=admin,
        )

    assert unapproved_url not in str(caught.value)
    assert smoke.registered == []
    assert admin.calls == []


def test_repository_coverage_lock_matches_provisioner_contract():
    coverage = Path(__file__).resolve().parents[1] / "coverage-lock.json"
    profiles = provisioner._load_coverage(coverage)
    assert [profile["profile_id"] for profile in profiles] == list(
        provisioner.PROFILE_SPECS
    )
    for profile in profiles:
        spec = provisioner.PROFILE_SPECS[profile["profile_id"]]
        assert profile["model_family"] == spec.model_family
        assert profile["model_env"] == spec.model_env
        assert profile["allowed_model_regex"] == spec.allowed_model_regex
        assert str(profile.get("base_url_env") or "") == spec.base_url_env
        assert str(profile.get("allowed_base_url") or "") == spec.allowed_base_url


@pytest.mark.parametrize("field", ["base_url_env", "allowed_base_url"])
def test_coverage_cannot_redirect_relay_key(tmp_path, field):
    rows = [dict(row) for row in PROFILE_ROWS]
    relay = next(row for row in rows if row["profile_id"] == "relay-kongbeiqie")
    relay[field] = (
        "QA_ATTACKER_URL" if field == "base_url_env" else "https://attacker.example/v1"
    )

    with pytest.raises(provisioner.ProvisionError, match="base URL") as caught:
        provisioner._load_coverage(_write_coverage(tmp_path, rows))

    assert "attacker.example" not in str(caught.value)


@pytest.mark.parametrize("profile_id", list(provisioner.PROFILE_SPECS))
def test_coverage_cannot_weaken_hard_coded_model_constraint(tmp_path, profile_id):
    rows = [dict(row) for row in PROFILE_ROWS]
    row = next(item for item in rows if item["profile_id"] == profile_id)
    row["allowed_model_regex"] = r"^.*$"

    with pytest.raises(provisioner.ProvisionError, match="model constraint mismatch"):
        provisioner._load_coverage(_write_coverage(tmp_path, rows))


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("model_family", "wrong-family", "model family mismatch"),
        ("model_env", "QA_WRONG_MODEL", "model environment mismatch"),
    ],
)
def test_coverage_model_route_fields_are_hard_locked(tmp_path, field, value, error):
    rows = [dict(row) for row in PROFILE_ROWS]
    rows[0][field] = value

    with pytest.raises(provisioner.ProvisionError, match=error):
        provisioner._load_coverage(_write_coverage(tmp_path, rows))


@pytest.mark.parametrize(
    "profile_id,model",
    [
        ("official-deepseek", "deepseek-chat"),
        ("official-deepseek", "deepseek-v4-flash"),
        ("official-anthropic", "claude-3-5-sonnet-latest"),
        ("official-anthropic", "claude-sonnet-4-5"),
        ("official-openai", "gpt-4o-mini"),
        ("official-openai", "gpt-5.4"),
        ("official-openai", "o1"),
        ("official-openai", "o3-mini"),
        ("official-gemini", "gemini-2.5-flash"),
        ("official-gemini", "gemini-2.5-pro"),
        ("openrouter-claude", "anthropic/claude-sonnet-4.5"),
        ("openrouter-openai", "openai/gpt-4.1-mini"),
        ("openrouter-openai", "openai/o3-mini"),
        ("openrouter-openai", "openai/o-series-preview"),
        ("openrouter-glm", "z-ai/glm-4.5-air:free"),
        ("openrouter-glm", "thudm/glm-4-32b"),
        ("relay-kongbeiqie", "claude-sonnet-4-6"),
        ("relay-kongbeiqie", "[特价纯血]claude-opus-4-6"),
    ],
)
def test_locked_model_families_accept_realistic_ids(profile_id, model):
    spec = provisioner.PROFILE_SPECS[profile_id]
    profile = next(row for row in PROFILE_ROWS if row["profile_id"] == profile_id)

    assert provisioner._model_for(profile, spec, {spec.model_env: model}) == model


@pytest.mark.parametrize(
    "profile_id,bad_model",
    [
        ("official-deepseek", "claude-sonnet-4-5"),
        ("official-anthropic", "deepseek-chat"),
        ("official-openai", "anthropic/claude-sonnet-4.5"),
        ("official-gemini", "gemini-2.0-flash"),
        ("official-gemini", "gemini-3.0-pro"),
        ("openrouter-claude", "openai/gpt-4.1-mini"),
        ("openrouter-openai", "z-ai/glm-4.5-air:free"),
        ("openrouter-glm", "anthropic/claude-sonnet-4.5"),
        ("relay-kongbeiqie", "openai/gpt-5.4"),
        ("relay-kongbeiqie", "[too-long-label-123456789012345678]claude-opus-4-6"),
    ],
)
def test_wrong_model_family_is_rejected_with_sanitized_error(profile_id, bad_model):
    spec = provisioner.PROFILE_SPECS[profile_id]
    profile = next(row for row in PROFILE_ROWS if row["profile_id"] == profile_id)

    with pytest.raises(provisioner.ProvisionError, match="locked family") as caught:
        provisioner._model_for(profile, spec, {spec.model_env: bad_model})

    assert bad_model not in str(caught.value)


@pytest.mark.parametrize(
    "bad_model",
    [
        "[line\nbreak]claude-opus-4-6",
        "[tab\tlabel]claude-opus-4-6",
        "[bidi\u202elabel]claude-opus-4-6",
        "[line\u2028break]claude-opus-4-6",
        "[pipe|label]claude-opus-4-6",
        "[back`tick]claude-opus-4-6",
    ],
)
def test_relay_model_rejects_controls_and_newlines_without_echo(bad_model):
    profile_id = "relay-kongbeiqie"
    spec = provisioner.PROFILE_SPECS[profile_id]
    profile = next(row for row in PROFILE_ROWS if row["profile_id"] == profile_id)

    with pytest.raises(provisioner.ProvisionError, match="locked family") as caught:
        provisioner._model_for(profile, spec, {spec.model_env: bad_model})

    assert bad_model not in str(caught.value)


@pytest.mark.parametrize(
    "left_profile,right_profile",
    [
        ("openrouter-claude", "openrouter-openai"),
        ("openrouter-claude", "openrouter-glm"),
        ("openrouter-openai", "openrouter-glm"),
    ],
)
def test_swapped_openrouter_models_fail_before_external_state(
    tmp_path, left_profile, right_profile
):
    env = _env()
    left = provisioner.PROFILE_SPECS[left_profile]
    right = provisioner.PROFILE_SPECS[right_profile]
    env[left.model_env], env[right.model_env] = (
        env[right.model_env],
        env[left.model_env],
    )
    smoke = FakeSmokeClient()
    admin = FakeAdminClient()

    with pytest.raises(provisioner.ProvisionError, match="locked family"):
        provisioner.provision(
            _write_coverage(tmp_path),
            tmp_path / "manifest.json",
            env=env,
            client=smoke,
            admin_client=admin,
        )

    assert smoke.registered == []
    assert admin.calls == []


def test_model_must_come_from_its_locked_environment_variable():
    profile_id = "official-deepseek"
    spec = provisioner.PROFILE_SPECS[profile_id]
    profile = next(row for row in PROFILE_ROWS if row["profile_id"] == profile_id)
    profile = {**profile, "configured_model": VALID_MODELS[profile_id]}

    with pytest.raises(provisioner.ProvisionError, match=spec.model_env):
        provisioner._model_for(profile, spec, {})


@pytest.mark.parametrize(
    "field,value",
    [
        ("reasoning_expected", False),
        ("reasoning_effort", ""),
        ("reasoning_effort", "high"),
    ],
)
def test_every_locked_profile_must_explicitly_enable_medium_reasoning(
    tmp_path, field, value
):
    rows = [dict(row) for row in PROFILE_ROWS]
    rows[0][field] = value
    with pytest.raises(provisioner.ProvisionError, match="reasoning"):
        provisioner.provision(
            _write_coverage(tmp_path, rows),
            tmp_path / "manifest.json",
            env=_env(),
            client=FakeSmokeClient(),
            admin_client=FakeAdminClient(),
        )


def test_invalid_key_acceptance_blocks_profiles_without_collapsing_matrix(tmp_path):
    smoke = FakeSmokeClient()
    smoke.accept_invalid = True
    manifest = tmp_path / "manifest.json"

    result = provisioner.provision(
        _write_coverage(tmp_path),
        manifest,
        env=_env(),
        client=smoke,
        admin_client=FakeAdminClient(),
    )

    assert len(smoke.registered) == 8
    assert [row["profile_id"] for row in result["profiles"]] == list(
        provisioner.PROFILE_SPECS
    )
    assert {row["provision_failure_code"] for row in result["profiles"]} == {
        "INVALID_KEY_ACCEPTED"
    }
    assert all(row["provision_status"] == "blocked" for row in result["profiles"])
    assert smoke.reset_calls == []
    assert manifest.exists()


def test_invalid_key_server_error_has_fixed_diagnostic_code(tmp_path):
    smoke = FakeSmokeClient()
    smoke.invalid_http_status = 503
    manifest = tmp_path / "manifest.json"

    result = provisioner.provision(
        _write_coverage(tmp_path),
        manifest,
        env=_env(),
        client=smoke,
        admin_client=FakeAdminClient(),
    )

    assert {row["provision_failure_code"] for row in result["profiles"]} == {
        "INVALID_KEY_REJECTION_FAILED"
    }
    assert smoke.reset_calls == []
    assert manifest.exists()


def test_invalid_key_response_must_not_echo_submitted_secret(tmp_path):
    smoke = FakeSmokeClient()
    smoke.echo_invalid_secret = True
    manifest = tmp_path / "manifest.json"

    result = provisioner.provision(
        _write_coverage(tmp_path),
        manifest,
        env=_env(),
        client=smoke,
        admin_client=FakeAdminClient(),
    )

    assert {row["provision_failure_code"] for row in result["profiles"]} == {
        "INVALID_KEY_ECHOED"
    }
    assert provisioner.INVALID_PROVIDER_KEY not in manifest.read_text()
    assert smoke.reset_calls == []


def test_expired_first_provider_key_does_not_abort_remaining_profiles(tmp_path):
    smoke = FakeSmokeClient()
    smoke.reject_valid_for = "user-0"
    env = _env()
    secret = env["QA_DEEPSEEK_API_KEY"]
    manifest = tmp_path / "manifest.json"

    result = provisioner.provision(
        _write_coverage(tmp_path),
        manifest,
        env=env,
        client=smoke,
        admin_client=FakeAdminClient(),
    )

    rows = result["profiles"]
    assert [row["profile_id"] for row in rows] == list(provisioner.PROFILE_SPECS)
    assert len(smoke.registered) == 8
    assert rows[0]["provision_status"] == provisioner.PROVISION_STATUS_BLOCKED
    assert rows[0]["provision_failure_code"] == "VALID_KEY_REJECTED"
    assert rows[0]["api_key"] == "feedling-account-key-0"
    assert rows[0]["secret_key_b64"]
    assert all(
        row["provision_status"] == provisioner.PROVISION_STATUS_READY
        and row["provision_failure_code"] == provisioner.PROVISION_FAILURE_NONE
        for row in rows[1:]
    )
    raw = manifest.read_text()
    assert secret not in raw
    assert "provider authentication rejected" not in raw
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert smoke.reset_calls == []


def test_valid_key_transport_failure_is_sanitized_and_isolated(tmp_path):
    smoke = FakeSmokeClient()
    smoke.fail_valid_for = "user-0"
    env = _env()
    secret = env["QA_DEEPSEEK_API_KEY"]
    manifest = tmp_path / "manifest.json"

    result = provisioner.provision(
        _write_coverage(tmp_path),
        manifest,
        env=env,
        client=smoke,
        admin_client=FakeAdminClient(),
    )

    assert result["profiles"][0]["provision_failure_code"] == "VALID_KEY_SETUP_FAILED"
    assert all(
        row["provision_status"] == provisioner.PROVISION_STATUS_READY
        for row in result["profiles"][1:]
    )
    raw = manifest.read_text()
    assert secret not in raw
    assert "provider echoed" not in raw


def test_valid_key_response_must_not_echo_submitted_secret(tmp_path):
    smoke = FakeSmokeClient()
    smoke.echo_valid_secret = True
    env = _env()
    manifest = tmp_path / "manifest.json"

    result = provisioner.provision(
        _write_coverage(tmp_path),
        manifest,
        env=env,
        client=smoke,
        admin_client=FakeAdminClient(),
    )

    assert {row["provision_failure_code"] for row in result["profiles"]} == {
        "VALID_KEY_ECHOED"
    }
    raw = manifest.read_text()
    for name, secret in env.items():
        if name.endswith("API_KEY") or name == "QA_TEST_ADMIN_TOKEN":
            assert secret not in raw
    assert smoke.reset_calls == []


def test_trace_must_be_deploy_enabled(tmp_path):
    smoke = FakeSmokeClient()
    smoke.trace_deploy_enabled = False
    result = provisioner.provision(
        _write_coverage(tmp_path),
        tmp_path / "manifest.json",
        env=_env(),
        client=smoke,
        admin_client=FakeAdminClient(),
    )
    assert {row["provision_failure_code"] for row in result["profiles"]} == {
        "TRACE_UNAVAILABLE"
    }
    assert smoke.reset_calls == []


def test_manifest_is_checkpointed_after_each_successful_profile_stage(
    tmp_path, monkeypatch
):
    snapshots = []
    original_write = provisioner._atomic_write_manifest

    def record_write(path, payload):
        snapshots.append(json.loads(json.dumps(payload)))
        original_write(path, payload)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    monkeypatch.setattr(provisioner, "_atomic_write_manifest", record_write)
    provisioner.provision(
        _write_coverage(tmp_path),
        tmp_path / "manifest.json",
        env=_env(),
        client=FakeSmokeClient(),
        admin_client=FakeAdminClient(),
    )

    assert len(snapshots) == 7 * len(provisioner.PROFILE_SPECS)
    first_profile_stages = [snapshot["profiles"][0] for snapshot in snapshots[:7]]
    assert first_profile_stages[0]["provision_failure_code"] == (
        provisioner.PROVISION_FAILURE_INCOMPLETE
    )
    assert first_profile_stages[1]["fresh_state_verified"] is True
    assert first_profile_stages[2]["invalid_key_rejected"] is True
    assert first_profile_stages[3]["valid_key_configured"] is True
    assert first_profile_stages[4]["trace_enabled"] is True
    assert first_profile_stages[5]["runtime_mode_set_verified"] is True
    assert first_profile_stages[6]["runtime_mode_readback_verified"] is True
    assert first_profile_stages[6]["provision_status"] == (
        provisioner.PROVISION_STATUS_READY
    )


def test_registration_failure_remains_global_and_cleans_prior_accounts(tmp_path):
    smoke = FakeSmokeClient()
    smoke.fail_registration_at = 1
    manifest = tmp_path / "manifest.json"

    with pytest.raises(
        provisioner.ProvisionError,
        match="account registration failed for profile: official-anthropic",
    ):
        provisioner.provision(
            _write_coverage(tmp_path),
            manifest,
            env=_env(),
            client=smoke,
            admin_client=FakeAdminClient(),
        )

    assert len(smoke.registered) == 1
    assert smoke.reset_calls == [
        ("feedling-account-key-0", {"confirm": "delete-all-data"})
    ]
    assert not manifest.exists()


def test_manifest_write_failure_still_cleans_registered_account(tmp_path, monkeypatch):
    smoke = FakeSmokeClient()

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(provisioner, "_atomic_write_manifest", fail_write)
    with pytest.raises(OSError, match="disk full"):
        provisioner.provision(
            _write_coverage(tmp_path),
            tmp_path / "manifest.json",
            env=_env(),
            client=smoke,
            admin_client=FakeAdminClient(),
        )
    assert smoke.reset_calls == [
        ("feedling-account-key-0", {"confirm": "delete-all-data"})
    ]


def test_cleanup_resets_every_account_and_removes_manifest(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_url": provisioner.ALLOWED_BASE_URL,
                "profiles": [
                    {"profile_id": "p1", "user_id": "u1", "api_key": "account-1"},
                    {"profile_id": "p2", "user_id": "u2", "api_key": "account-2"},
                ],
            }
        )
    )
    smoke = FakeSmokeClient()

    result = provisioner.cleanup(manifest_path, client=smoke)

    assert result == {
        "attempted": 2,
        "cleaned": 2,
        "failed_profile_ids": [],
        "manifest_deleted": True,
        "manifest_missing": False,
    }
    assert [call[0] for call in smoke.reset_calls] == ["account-1", "account-2"]
    assert all(call[1] == {"confirm": "delete-all-data"} for call in smoke.reset_calls)
    assert not manifest_path.exists()


def test_cleanup_failure_keeps_manifest_for_retry(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_url": provisioner.ALLOWED_BASE_URL,
                "profiles": [
                    {"profile_id": "p1", "user_id": "u1", "api_key": "account-1"},
                ],
            }
        )
    )
    smoke = FakeSmokeClient()
    smoke.reset_fail_for.add("account-1")

    result = provisioner.cleanup(manifest_path, client=smoke)

    assert result["failed_profile_ids"] == ["p1"]
    assert result["manifest_deleted"] is False
    assert manifest_path.exists()


def test_cleanup_treats_already_reset_401_as_success(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_url": provisioner.ALLOWED_BASE_URL,
                "profiles": [
                    {"profile_id": "p1", "user_id": "u1", "api_key": "account-1"},
                ],
            }
        )
    )
    smoke = FakeSmokeClient()
    smoke.already_reset_for.add("account-1")
    admin = FakeAdminClient()
    admin.missing_users.add("u1")

    result = provisioner.cleanup(manifest_path, client=smoke, admin_client=admin)

    assert result["cleaned"] == 1
    assert result["failed_profile_ids"] == []
    assert result["manifest_deleted"] is True
    assert not manifest_path.exists()
    assert admin.calls == [("GET", "/v1/admin/data-track/users/u1", None)]


def test_cleanup_401_without_admin_proof_keeps_manifest(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_url": provisioner.ALLOWED_BASE_URL,
                "profiles": [
                    {"profile_id": "p1", "user_id": "u1", "api_key": "account-1"},
                ],
            }
        )
    )
    smoke = FakeSmokeClient()
    smoke.already_reset_for.add("account-1")

    result = provisioner.cleanup(manifest_path, env={}, client=smoke)

    assert result["cleaned"] == 0
    assert result["failed_profile_ids"] == ["p1"]
    assert result["manifest_deleted"] is False
    assert manifest_path.exists()


def test_cleanup_401_with_existing_admin_user_keeps_manifest(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_url": provisioner.ALLOWED_BASE_URL,
                "profiles": [
                    {"profile_id": "p1", "user_id": "u1", "api_key": "account-1"},
                ],
            }
        )
    )
    smoke = FakeSmokeClient()
    smoke.already_reset_for.add("account-1")
    admin = FakeAdminClient()

    result = provisioner.cleanup(manifest_path, client=smoke, admin_client=admin)

    assert result["cleaned"] == 0
    assert result["failed_profile_ids"] == ["p1"]
    assert result["manifest_deleted"] is False
    assert manifest_path.exists()


def test_cleanup_missing_manifest_is_idempotent(tmp_path):
    result = provisioner.cleanup(tmp_path / "absent.json", client=FakeSmokeClient())
    assert result["manifest_missing"] is True
    assert result["attempted"] == 0


def test_provision_cli_succeeds_for_complete_matrix_with_blocked_profile(
    tmp_path, monkeypatch, capsys
):
    coverage = _write_coverage(tmp_path)
    manifest = tmp_path / "manifest.json"
    smoke = FakeSmokeClient()
    smoke.reject_valid_for = "user-0"
    env = _env()
    original_provision = provisioner.provision

    def injected_provision(coverage_path, manifest_path):
        return original_provision(
            coverage_path,
            manifest_path,
            env=env,
            client=smoke,
            admin_client=FakeAdminClient(),
        )

    monkeypatch.setattr(provisioner, "provision", injected_provision)
    exit_code = provisioner.main(
        ["provision", "--coverage", str(coverage), "--manifest", str(manifest)]
    )
    captured = capsys.readouterr()
    output = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert output == {
        "ok": True,
        "profile_count": 8,
        "ready_profile_count": 7,
        "blocked_profile_count": 1,
        "blocked_profile_ids": ["official-deepseek"],
        "manifest": str(manifest),
    }
    raw = manifest.read_text()
    assert env["QA_DEEPSEEK_API_KEY"] not in raw
    assert [row["profile_id"] for row in json.loads(raw)["profiles"]] == list(
        provisioner.PROFILE_SPECS
    )


def test_provision_cli_is_nonzero_when_registration_prevents_complete_manifest(
    tmp_path, monkeypatch, capsys
):
    coverage = _write_coverage(tmp_path)
    manifest = tmp_path / "manifest.json"
    smoke = FakeSmokeClient()
    smoke.fail_registration_at = 1
    env = _env()
    original_provision = provisioner.provision

    def injected_provision(coverage_path, manifest_path):
        return original_provision(
            coverage_path,
            manifest_path,
            env=env,
            client=smoke,
            admin_client=FakeAdminClient(),
        )

    monkeypatch.setattr(provisioner, "provision", injected_provision)
    exit_code = provisioner.main(
        ["provision", "--coverage", str(coverage), "--manifest", str(manifest)]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.out == ""
    assert captured.err == (
        "provisioning error: account registration failed for profile: "
        "official-anthropic\n"
    )
    assert all(secret not in captured.err for secret in env.values())
    assert not manifest.exists()


def test_cleanup_cli_emits_machine_readable_sanitized_summary(tmp_path, capsys):
    exit_code = provisioner.main(
        ["cleanup", "--manifest", str(tmp_path / "absent.json")]
    )
    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output == {
        "ok": True,
        "attempted": 0,
        "cleaned": 0,
        "failed_profile_ids": [],
        "manifest_deleted": False,
    }
