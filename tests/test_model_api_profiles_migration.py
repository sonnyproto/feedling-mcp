"""Tests for the 0014 migration's backfill: user_blobs(kind='model_api') ->
model_api_credentials + model_api_routes.

Task 1 scope only: db.model_api_credentials_list()/model_api_routes_list()
(Task 2) don't exist yet, so assertions go through raw SQL against the two
new tables instead. Requires a real PostgreSQL — see tests/conftest.py,
which provisions a throwaway DB and runs `alembic upgrade head` (so both
tables already exist by the time these tests run) before any module is
collected.
"""

import importlib.util
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import db  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

from conftest import seed_user  # noqa: E402

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "backend"
    / "alembic"
    / "versions"
    / "0014_model_api_profiles.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig0014", _MIGRATION_PATH)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    return mig


_mig = _load_migration()


def _uid() -> str:
    return f"usr_{uuid.uuid4().hex[:16]}"


def _run_backfill() -> None:
    with db.get_pool().connection() as conn:
        conn.execute(_mig.BACKFILL_SQL)


def _seed_blob(user_id: str, doc: dict) -> None:
    seed_user(user_id)
    db.set_blob(user_id, "model_api", doc)


def _credentials(user_id: str):
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT provider, label, base_url, api_key_hint, supports_responses, "
            "api_key_envelope FROM model_api_credentials WHERE user_id = %s",
            (user_id,),
        ).fetchall()


def _routes(user_id: str):
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT model, reasoning_effort, is_active, test_status "
            "FROM model_api_routes WHERE user_id = %s",
            (user_id,),
        ).fetchall()


def test_backfill_creates_one_credential_and_one_active_route():
    uid = _uid()
    _seed_blob(uid, {
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "base_url": "",
        "api_key_envelope": {"v": 1, "body_ct": "abc"},
        "api_key_hint": "sk-a...451",
        "test_status": "ok",
        "reasoning_effort": "high",
        "supports_responses": "false",
    })

    _run_backfill()

    creds = _credentials(uid)
    assert len(creds) == 1
    provider, label, base_url, api_key_hint, supports_responses, envelope = creds[0]
    assert provider == "anthropic"
    assert label == "Anthropic"
    assert base_url == ""
    assert api_key_hint == "sk-a...451"
    assert supports_responses is False
    assert envelope == {"v": 1, "body_ct": "abc"}

    routes = _routes(uid)
    assert len(routes) == 1
    model, reasoning_effort, is_active, test_status = routes[0]
    assert model == "claude-sonnet-4-5"
    assert is_active is True
    assert test_status == "ok"
    assert reasoning_effort == "high"

    # user_blobs is the rollback snapshot — the migration must not touch it.
    assert db.get_blob(uid, "model_api") is not None


def test_backfill_is_idempotent():
    uid = _uid()
    _seed_blob(uid, {
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "base_url": "",
        "api_key_envelope": {"v": 1, "body_ct": "abc"},
        "test_status": "ok",
    })

    _run_backfill()
    _run_backfill()

    assert len(_credentials(uid)) == 1
    assert len(_routes(uid)) == 1


def test_same_provider_and_base_url_allows_two_credentials():
    """Core protection for this design: iOS lists multiple keys under the same
    provider+base_url, so the table must NOT have a (user_id, provider,
    base_url) unique index. Inserting a second row with identical
    provider+base_url must succeed."""
    uid = _uid()
    seed_user(uid)
    with db.get_pool().connection() as conn:
        for hint in ("...31AF", "...D982"):
            conn.execute(
                "INSERT INTO model_api_credentials "
                "(id, user_id, provider, base_url, api_key_envelope, api_key_hint) "
                "VALUES (gen_random_uuid(), %s, 'openai', '', %s, %s)",
                (uid, Jsonb({"v": 1, "body_ct": "abc"}), hint),
            )
    creds = _credentials(uid)
    assert len(creds) == 2
    assert {c[3] for c in creds} == {"...31AF", "...D982"}


def test_backfill_skips_user_who_already_has_a_credential():
    """Per-user NOT EXISTS guard: a user who already owns a credential is
    treated as already-migrated — re-running the backfill inserts neither a
    second credential nor a partial route (both INSERTs are symmetric now)."""
    uid = _uid()
    _seed_blob(uid, {
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "api_key_envelope": {"v": 1, "body_ct": "abc"},
    })
    # Pre-existing credential (e.g. created by a later setup endpoint), NOT the
    # one the backfill would insert — different hint so we can tell them apart.
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO model_api_credentials "
            "(id, user_id, provider, base_url, api_key_envelope, api_key_hint) "
            "VALUES (gen_random_uuid(), %s, 'openai', '', %s, 'preexisting')",
            (uid, Jsonb({"v": 1, "body_ct": "zzz"})),
        )

    _run_backfill()

    creds = _credentials(uid)
    assert len(creds) == 1
    assert creds[0][3] == "preexisting"
    # No route backfilled for an already-migrated user (no silent partial insert).
    assert _routes(uid) == []


def test_backfill_does_not_abort_or_duplicate_when_user_has_active_route():
    """Symmetric routes guard (Important 1): a user who already has an active
    route must not trip model_api_routes_one_active when the backfill re-runs.
    Without the routes NOT EXISTS guard the second is_active=TRUE route would
    abort the whole statement. Must skip cleanly and preserve the existing
    route."""
    uid = _uid()
    _seed_blob(uid, {
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "api_key_envelope": {"v": 1, "body_ct": "abc"},
    })
    # Pre-existing credential with the SAME provider+base_url as the blob (so the
    # routes JOIN would match it), plus an existing ACTIVE route on it.
    with db.get_pool().connection() as conn:
        cid = conn.execute(
            "INSERT INTO model_api_credentials "
            "(id, user_id, provider, base_url, api_key_envelope, api_key_hint) "
            "VALUES (gen_random_uuid(), %s, 'anthropic', '', %s, 'preexisting') "
            "RETURNING id",
            (uid, Jsonb({"v": 1, "body_ct": "zzz"})),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO model_api_routes "
            "(id, user_id, credential_id, model, is_active) "
            "VALUES (gen_random_uuid(), %s, %s, 'old-model', TRUE)",
            (uid, cid),
        )

    # Must not raise (no partial-unique-index abort).
    _run_backfill()

    routes = _routes(uid)
    assert len(routes) == 1
    model, _reasoning, is_active, _status = routes[0]
    assert model == "old-model"
    assert is_active is True
    # And no second credential was created either.
    assert len(_credentials(uid)) == 1


def test_backfill_skips_blob_without_envelope():
    uid = _uid()
    _seed_blob(uid, {"provider": "openai", "model": "gpt-4.1-mini", "test_status": "failed"})

    _run_backfill()

    assert _credentials(uid) == []
    assert _routes(uid) == []


def test_backfill_defaults_missing_reasoning_effort_and_test_status():
    uid = _uid()
    _seed_blob(uid, {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "api_key_envelope": {"v": 1, "body_ct": "xyz"},
    })

    _run_backfill()

    routes = _routes(uid)
    assert len(routes) == 1
    model, reasoning_effort, is_active, test_status = routes[0]
    assert model == "deepseek-chat"
    assert reasoning_effort is None
    assert is_active is True
    assert test_status == "untested"


def test_backfill_label_titlecases_underscore_provider():
    """Minor: INITCAP treats '_' as a word boundary, so use REPLACE first to
    match setup_core's provider.replace('_',' ').title() -> 'Openai Compatible'
    (not 'Openai_Compatible')."""
    uid = _uid()
    _seed_blob(uid, {
        "provider": "openai_compatible",
        "model": "some-model",
        "api_key_envelope": {"v": 1, "body_ct": "abc"},
    })

    _run_backfill()

    creds = _credentials(uid)
    assert len(creds) == 1
    provider, label = creds[0][0], creds[0][1]
    assert provider == "openai_compatible"
    assert label == "Openai Compatible"
