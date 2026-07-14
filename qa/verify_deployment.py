#!/usr/bin/env python3
"""Create a trusted deployed-runtime receipt before Codex runs.

The headless qualification agent must not be the authority for its own target.
Baseline mode proves only that the designated test endpoint is live. Strict V2
mode additionally reads the admin-gated V2 metrics endpoint, requires one exact
backend build and one homogeneous live-worker build, and checkpoints a read-only
receipt outside the agent's writable artifact directory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from qa.provision_profiles import (  # noqa: E402
    AdminClient,
    ProvisionError,
    validate_base_url,
)


_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
RECEIPT_SCHEMA_VERSION = 1
BASELINE_RUNTIME = "deployed_current"
RUNTIME_V2_RUNTIME = "hosted_resident"


class DeploymentVerificationError(RuntimeError):
    """A fixed deployment-preflight failure safe to print in CI."""


def _required_env(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name) or "").strip()
    if not value:
        raise DeploymentVerificationError(
            f"missing required environment variable: {name}"
        )
    return value


def _write_read_only_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o400)
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def verify_deployment(
    expected_sha: str,
    receipt_path: Path,
    *,
    env: Mapping[str, str] | None = None,
    admin_client: AdminClient | None = None,
    expected_runtime: str = RUNTIME_V2_RUNTIME,
) -> dict[str, Any]:
    active_env = os.environ if env is None else env
    expected = str(expected_sha or "").strip().lower()
    if not _SHA_RE.fullmatch(expected):
        raise DeploymentVerificationError("expected deployment SHA is malformed")
    if expected_runtime not in {BASELINE_RUNTIME, RUNTIME_V2_RUNTIME}:
        raise DeploymentVerificationError("runtime requirement is invalid")
    base_url = validate_base_url(_required_env(active_env, "QA_FEEDLING_BASE_URL"))
    token = _required_env(active_env, "QA_TEST_ADMIN_TOKEN")
    client = admin_client or AdminClient(base_url, token)

    if expected_runtime == BASELINE_RUNTIME:
        try:
            status, payload = client.request("GET", "/healthz")
        except ProvisionError:
            raise DeploymentVerificationError(
                "test deployment health endpoint was unreachable"
            ) from None
        if (
            status != 200
            or not isinstance(payload, dict)
            or payload.get("ok") is not True
        ):
            raise DeploymentVerificationError(
                "test deployment health endpoint is unavailable"
            )
        receipt = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "environment": "test",
            "base_url": base_url,
            "expected_runtime": BASELINE_RUNTIME,
            "expected_deployment_sha": expected,
            "observed_backend_sha": None,
            "observed_worker_sha": None,
            "live_worker_count": None,
            "liveness_verified": True,
            "deployment_identity_verified": False,
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            _write_read_only_json(receipt_path, receipt)
        except OSError:
            raise DeploymentVerificationError(
                "deployment receipt could not be checkpointed"
            ) from None
        return receipt

    try:
        status, payload = client.request("GET", "/v1/admin/v2-metrics")
    except ProvisionError:
        raise DeploymentVerificationError(
            "V2 deployment identity endpoint was unreachable"
        ) from None
    if status != 200 or not isinstance(payload, dict):
        raise DeploymentVerificationError(
            "V2 deployment identity endpoint is unavailable"
        )

    backend_sha = str(payload.get("backend_sha") or "").strip().lower()
    worker_shas_raw = payload.get("worker_shas")
    live_workers = payload.get("live_workers")
    if not _SHA_RE.fullmatch(backend_sha):
        raise DeploymentVerificationError(
            "V2 metrics has no valid backend build identity"
        )
    if not isinstance(worker_shas_raw, list) or not worker_shas_raw:
        raise DeploymentVerificationError(
            "V2 metrics has no live worker build identity"
        )
    worker_shas_normalized = [
        value.strip().lower() if isinstance(value, str) else ""
        for value in worker_shas_raw
    ]
    worker_shas = set(worker_shas_normalized)
    if (
        not isinstance(live_workers, int)
        or isinstance(live_workers, bool)
        or live_workers < 1
        or len(worker_shas_raw) != live_workers
        or len(worker_shas) != 1
        or any(not _SHA_RE.fullmatch(value) for value in worker_shas_normalized)
    ):
        raise DeploymentVerificationError(
            "V2 worker build identity is incomplete or heterogeneous"
        )
    worker_sha = next(iter(worker_shas))
    if backend_sha != expected or worker_sha != expected:
        raise DeploymentVerificationError(
            "deployed backend and worker builds do not match the candidate"
        )

    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "environment": "test",
        "base_url": base_url,
        "expected_runtime": RUNTIME_V2_RUNTIME,
        "expected_deployment_sha": expected,
        "observed_backend_sha": backend_sha,
        "observed_worker_sha": worker_sha,
        "live_worker_count": live_workers,
        "liveness_verified": True,
        "deployment_identity_verified": True,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _write_read_only_json(receipt_path, receipt)
    except OSError:
        raise DeploymentVerificationError(
            "deployment receipt could not be checkpointed"
        ) from None
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument(
        "--expected-runtime",
        choices=(BASELINE_RUNTIME, RUNTIME_V2_RUNTIME),
        default=RUNTIME_V2_RUNTIME,
    )
    parser.add_argument("--receipt", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = verify_deployment(
            args.expected_sha,
            args.receipt,
            expected_runtime=args.expected_runtime,
        )
    except (DeploymentVerificationError, ProvisionError) as exc:
        print(f"deployment verification error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "expected_runtime": receipt["expected_runtime"],
                "live_worker_count": receipt["live_worker_count"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
