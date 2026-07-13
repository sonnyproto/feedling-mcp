from __future__ import annotations

import json
import stat

import pytest

from qa import verify_deployment as deployment


SHA = "a" * 40
ENV = {
    "QA_FEEDLING_BASE_URL": "https://test-api.feedling.app",
    "QA_TEST_ADMIN_TOKEN": "test-admin-token",
}


class FakeAdmin:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = payload or {
            "backend_sha": SHA,
            "worker_shas": [SHA, SHA],
            "live_workers": 2,
        }
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self.status, self.payload


def test_trusted_receipt_requires_matching_backend_and_workers(tmp_path):
    receipt_path = tmp_path / "deployment.json"
    fake = FakeAdmin()
    receipt = deployment.verify_deployment(
        SHA, receipt_path, env=ENV, admin_client=fake
    )
    assert fake.calls == [("GET", "/v1/admin/v2-metrics", None)]
    assert receipt["observed_backend_sha"] == SHA
    assert receipt["observed_worker_sha"] == SHA
    assert json.loads(receipt_path.read_text())["live_worker_count"] == 2
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o400


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"backend_sha": "", "worker_shas": [SHA], "live_workers": 1},
            "no valid backend",
        ),
        (
            {"backend_sha": SHA, "worker_shas": [], "live_workers": 1},
            "no live worker",
        ),
        (
            {"backend_sha": SHA, "worker_shas": [SHA, "b" * 40], "live_workers": 2},
            "heterogeneous",
        ),
        (
            {"backend_sha": SHA, "worker_shas": [SHA, None], "live_workers": 2},
            "heterogeneous",
        ),
        (
            {"backend_sha": SHA, "worker_shas": [SHA], "live_workers": 99},
            "heterogeneous",
        ),
        (
            {"backend_sha": SHA, "worker_shas": [SHA], "live_workers": 0},
            "heterogeneous",
        ),
        (
            {"backend_sha": "b" * 40, "worker_shas": [SHA], "live_workers": 1},
            "do not match",
        ),
    ],
)
def test_invalid_or_mixed_deployment_identity_fails(tmp_path, payload, message):
    with pytest.raises(deployment.DeploymentVerificationError, match=message):
        deployment.verify_deployment(
            SHA,
            tmp_path / "deployment.json",
            env=ENV,
            admin_client=FakeAdmin(payload=payload),
        )


def test_unavailable_endpoint_and_missing_inputs_fail_closed(tmp_path):
    with pytest.raises(deployment.DeploymentVerificationError, match="unavailable"):
        deployment.verify_deployment(
            SHA,
            tmp_path / "deployment.json",
            env=ENV,
            admin_client=FakeAdmin(status=404),
        )
    with pytest.raises(
        deployment.DeploymentVerificationError, match="QA_TEST_ADMIN_TOKEN"
    ):
        deployment.verify_deployment(
            SHA,
            tmp_path / "deployment.json",
            env={"QA_FEEDLING_BASE_URL": ENV["QA_FEEDLING_BASE_URL"]},
            admin_client=FakeAdmin(),
        )
