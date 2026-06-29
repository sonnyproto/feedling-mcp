"""Cloudflare R2 storage for onboarding original-file archives.

Like diagnostics logs (``diagnostics/storage.py``), these are stored
**plaintext** — a deliberate, scoped exception to the "server never sees user
plaintext" invariant: the user's onboarding upload is already plaintext on the
history_import path, and we keep the original for audit / re-processing. We
borrow ``object_storage``'s shared boto3 client + R2 credentials but use the
``io-user-logs`` bucket under a dedicated ``onboarding/`` key prefix.

Crypto note: R2 encrypts all objects at rest by default (transparent SSE). That
protects the on-disk blob, NOT API reads — anyone holding the R2 credentials
gets plaintext back. Same trust level as diagnostics logs / history_import.

Config (no new secrets — reuses the repo's existing R2 setup):
  - ``R2_ENDPOINT`` / ``R2_ACCESS_KEY_ID`` / ``R2_SECRET_ACCESS_KEY``
  - ``R2_USER_LOGS_BUCKET`` : the shared client-logs bucket (``io-user-logs``).
"""

from __future__ import annotations

import logging
import os

import object_storage

log = logging.getLogger("feedling.onboarding_archive")

_PREFIX = "onboarding"


def _bucket() -> str:
    return os.environ.get("R2_USER_LOGS_BUCKET", "").strip()


def enabled() -> bool:
    """True only when R2 credentials and the user-logs bucket are all present."""
    return bool(object_storage.credentials_present() and _bucket())


def archive_key(user_id: str, archive_id: str, safe_filename: str) -> str:
    return f"{_PREFIX}/{user_id}/{archive_id}/{safe_filename}"


def put_archive(user_id: str, archive_id: str, safe_filename: str,
                fileobj, content_type: str) -> str:
    """Stream the upload straight to R2 (no full read into memory); return the
    object key. Raises on failure so the route can surface 502."""
    key = archive_key(user_id, archive_id, safe_filename)
    object_storage.client().upload_fileobj(
        fileobj,
        _bucket(),
        key,
        ExtraArgs={"ContentType": content_type or "application/octet-stream"},
    )
    return key


def delete_user_archives(user_id: str) -> None:
    """Delete every object under ``onboarding/<user_id>/`` (account reset).

    Raises on R2 failure so a privacy-critical caller (account reset) can abort
    rather than silently leave plaintext originals behind on R2."""
    prefix = f"{_PREFIX}/{user_id}/"
    client = object_storage.client()
    bucket = _bucket()
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = client.list_objects_v2(**kwargs)
        objs = [{"Key": c["Key"]} for c in resp.get("Contents", [])]
        if objs:
            del_resp = client.delete_objects(Bucket=bucket, Delete={"Objects": objs})
            errors = del_resp.get("Errors") or []
            if errors:
                raise RuntimeError(
                    f"R2 delete_objects reported {len(errors)} error(s) for "
                    f"onboarding/{user_id}/: {errors[:3]}"
                )
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
