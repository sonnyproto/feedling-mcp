"""Cloudflare R2 storage for client diagnostic logs.

Unlike screen frames (``object_storage.py``), diagnostic logs are uploaded as
**plaintext** — a deliberate, scoped exception to the "server never sees user
plaintext" invariant: the user taps an explicit "upload diagnostics" action,
the testers are few, and the bucket is private with a short retention. We keep
this plaintext concern out of ``object_storage.py`` (whose contract is
ciphertext-only) and only borrow its shared boto3 client + R2 credentials.

Config (reuses the repo's existing ``R2_*`` credentials):
  - ``R2_ENDPOINT`` / ``R2_ACCESS_KEY_ID`` / ``R2_SECRET_ACCESS_KEY``
  - ``R2_USER_LOGS_BUCKET`` : a dedicated bucket for client logs (``io-user-logs``),
    separate from the frames / WAL-G backup buckets.

When the bucket or credentials are unset, ``enabled()`` returns False and the
route falls back to storing the log text inline in Postgres, so local dev /
tests work without R2.
"""

from __future__ import annotations

import logging
import os

import object_storage

log = logging.getLogger("feedling.diagnostics")


def _bucket() -> str:
    return os.environ.get("R2_USER_LOGS_BUCKET", "").strip()


def enabled() -> bool:
    """True only when R2 credentials and the user-logs bucket are all present."""
    return bool(object_storage.credentials_present() and _bucket())


def log_key(user_id: str, ts_iso: str) -> str:
    # Dedicated bucket, so group by user only — no extra prefix needed.
    return f"{user_id}/{ts_iso}.log"


def put_log(user_id: str, ts_iso: str, text: str) -> str:
    """Upload the plaintext log; return the R2 object key. Raises on failure so
    the caller can fall back to inline Postgres storage."""
    key = log_key(user_id, ts_iso)
    object_storage.client().put_object(
        Bucket=_bucket(),
        Key=key,
        Body=text.encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )
    return key


def presign_get(key: str, expires: int = 3600) -> str | None:
    """A short-lived presigned GET URL so an admin can download the .log
    directly. Returns None on failure (the caller still surfaces the metadata)."""
    try:
        return object_storage.client().generate_presigned_url(
            "get_object",
            Params={"Bucket": _bucket(), "Key": key},
            ExpiresIn=expires,
        )
    except Exception as e:  # noqa: BLE001
        log.error("[r2] presign_get(%s) failed: %s", key, e)
        return None
