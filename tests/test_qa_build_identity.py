"""Pure contract tests for the test-only authoritative build identity."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin import qa_build_identity as identity


SHA = "a" * 40


def _enable(monkeypatch, *, image_sha: str = SHA, deployment_sha: str = SHA) -> None:
    monkeypatch.setenv(identity.SYNTHETIC_ENABLED_ENV, "true")
    monkeypatch.setenv(identity.IMAGE_SHA_ENV, image_sha)
    monkeypatch.setenv(identity.DEPLOY_SHA_ENV, deployment_sha)


def test_identity_requires_matching_full_image_and_deploy_shas(monkeypatch):
    _enable(monkeypatch)

    assert identity.status_payload() == {
        "schema_version": 1,
        "environment": "test",
        "backend_sha": SHA,
        "deployment_sha": SHA,
        "identity_verified": True,
    }


@pytest.mark.parametrize(
    ("image_sha", "deployment_sha"),
    (
        ("a" * 7, "a" * 7),
        (SHA, "b" * 40),
        ("not-a-sha", SHA),
        (SHA, ""),
    ),
)
def test_identity_fails_closed_for_missing_invalid_or_mismatched_shas(
    monkeypatch, image_sha, deployment_sha
):
    _enable(monkeypatch, image_sha=image_sha, deployment_sha=deployment_sha)

    with pytest.raises(identity.BuildIdentityUnavailable):
        identity.status_payload()


def test_identity_is_unavailable_when_test_synthetic_mode_is_disabled(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv(identity.SYNTHETIC_ENABLED_ENV, "false")

    with pytest.raises(identity.BuildIdentityUnavailable):
        identity.status_payload()
