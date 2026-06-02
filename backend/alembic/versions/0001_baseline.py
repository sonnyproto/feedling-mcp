"""baseline schema (all tables as of the file→Postgres migration)

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-02

This baseline captures the full schema that db.init_schema() used to create
inline. The DDL is idempotent (CREATE TABLE/INDEX IF NOT EXISTS) so that
stamping it onto the already-provisioned production RDS — whose tables were
created by the old init_schema() before Alembic existed — is a safe no-op that
just records the version. New schema changes go in fresh revisions on top.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


_BASELINE_DDL = """
CREATE TABLE IF NOT EXISTS server_config (
    key   TEXT PRIMARY KEY,
    value BYTEA NOT NULL
);

CREATE TABLE IF NOT EXISTS global_blobs (
    key TEXT PRIMARY KEY,
    doc JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    created_at TEXT,
    doc        JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS user_blobs (
    user_id TEXT NOT NULL,
    kind    TEXT NOT NULL,
    doc     JSONB NOT NULL,
    PRIMARY KEY (user_id, kind)
);

CREATE TABLE IF NOT EXISTS chat_messages (
    user_id TEXT NOT NULL,
    seq     BIGINT GENERATED ALWAYS AS IDENTITY,
    msg_id  TEXT NOT NULL,
    ts      DOUBLE PRECISION NOT NULL,
    doc     JSONB NOT NULL,
    PRIMARY KEY (user_id, msg_id)
);
CREATE INDEX IF NOT EXISTS chat_user_seq_idx ON chat_messages (user_id, seq);

CREATE TABLE IF NOT EXISTS memory_moments (
    user_id     TEXT NOT NULL,
    moment_id   TEXT NOT NULL,
    occurred_at TEXT NOT NULL DEFAULT '',
    doc         JSONB NOT NULL,
    PRIMARY KEY (user_id, moment_id)
);
CREATE INDEX IF NOT EXISTS memory_user_occ_idx ON memory_moments (user_id, occurred_at);

CREATE TABLE IF NOT EXISTS frame_envelopes (
    user_id  TEXT NOT NULL,
    frame_id TEXT NOT NULL,
    ts       DOUBLE PRECISION NOT NULL,
    doc      JSONB NOT NULL,
    PRIMARY KEY (user_id, frame_id)
);
CREATE INDEX IF NOT EXISTS frame_user_ts_idx ON frame_envelopes (user_id, ts);

CREATE TABLE IF NOT EXISTS user_logs (
    user_id  TEXT NOT NULL,
    stream   TEXT NOT NULL,
    seq      BIGINT GENERATED ALWAYS AS IDENTITY,
    ts       DOUBLE PRECISION,
    item_key TEXT,
    doc      JSONB NOT NULL,
    PRIMARY KEY (user_id, stream, seq)
);
CREATE INDEX IF NOT EXISTS logs_stream_seq_idx ON user_logs (user_id, stream, seq);
CREATE INDEX IF NOT EXISTS logs_stream_ts_idx  ON user_logs (user_id, stream, ts);
CREATE INDEX IF NOT EXISTS logs_item_key_idx
    ON user_logs (user_id, stream, item_key) WHERE item_key IS NOT NULL;
"""

_DROP_DDL = """
DROP TABLE IF EXISTS user_logs;
DROP TABLE IF EXISTS frame_envelopes;
DROP TABLE IF EXISTS memory_moments;
DROP TABLE IF EXISTS chat_messages;
DROP TABLE IF EXISTS user_blobs;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS global_blobs;
DROP TABLE IF EXISTS server_config;
"""


def upgrade() -> None:
    op.execute(_BASELINE_DDL)


def downgrade() -> None:
    op.execute(_DROP_DDL)
