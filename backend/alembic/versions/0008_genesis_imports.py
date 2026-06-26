"""genesis import ledger: jobs, chunks, reducer outputs

Revision ID: 0008_genesis_imports
Revises: 0007_frame_body_to_r2
Create Date: 2026-06-27

Genesis imports are resumable, chunked uploads. The backend stores encrypted
chunk bytes plus plaintext ledger metadata; reducer outputs are stored
separately so retries are idempotent and the runtime gate can read one durable
job status instead of inspecting per-spawn homes.

DDL is idempotent (IF NOT EXISTS) to match the baseline's safety property.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0008_genesis_imports"
down_revision = "0007_frame_body_to_r2"
branch_labels = None
depends_on = None


_DDL = """
CREATE TABLE IF NOT EXISTS genesis_import_jobs (
    user_id             TEXT NOT NULL,
    job_id              TEXT NOT NULL,
    status              TEXT NOT NULL,
    source_kind          TEXT NOT NULL DEFAULT 'unknown',
    file_manifest_hash  TEXT NOT NULL DEFAULT '',
    total_chunks        INTEGER NOT NULL DEFAULT 0,
    received_chunks     INTEGER NOT NULL DEFAULT 0,
    processed_chunks    INTEGER NOT NULL DEFAULT 0,
    total_bytes         BIGINT NOT NULL DEFAULT 0,
    received_bytes      BIGINT NOT NULL DEFAULT 0,
    privacy_mode        TEXT NOT NULL DEFAULT '',
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    output              JSONB NOT NULL DEFAULT '{}'::jsonb,
    memory_action_count INTEGER NOT NULL DEFAULT 0,
    identity_status     TEXT NOT NULL DEFAULT '',
    persona_ref         TEXT NOT NULL DEFAULT '',
    persona_sha256      TEXT NOT NULL DEFAULT '',
    error               TEXT NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finalized_at        TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    PRIMARY KEY (user_id, job_id)
);
CREATE INDEX IF NOT EXISTS genesis_jobs_user_status_idx
    ON genesis_import_jobs (user_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS genesis_jobs_ready_idx
    ON genesis_import_jobs (user_id, completed_at DESC)
    WHERE status = 'done';

CREATE TABLE IF NOT EXISTS genesis_import_chunks (
    user_id            TEXT NOT NULL,
    job_id             TEXT NOT NULL,
    seq                INTEGER NOT NULL,
    byte_start         BIGINT NOT NULL DEFAULT 0,
    byte_end           BIGINT NOT NULL DEFAULT 0,
    ciphertext_sha256  TEXT NOT NULL,
    content_sha256     TEXT NOT NULL DEFAULT '',
    aad                JSONB NOT NULL DEFAULT '{}'::jsonb,
    encrypted_body     BYTEA NOT NULL,
    size_bytes         INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'uploaded',
    attempts           INTEGER NOT NULL DEFAULT 0,
    map_output_ref     TEXT NOT NULL DEFAULT '',
    error              TEXT NOT NULL DEFAULT '',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, job_id, seq),
    FOREIGN KEY (user_id, job_id)
        REFERENCES genesis_import_jobs (user_id, job_id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS genesis_chunks_job_idx
    ON genesis_import_chunks (user_id, job_id, seq);

CREATE TABLE IF NOT EXISTS genesis_import_outputs (
    user_id      TEXT NOT NULL,
    job_id       TEXT NOT NULL,
    output_type  TEXT NOT NULL,
    ref          TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT '',
    doc          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, job_id, output_type),
    FOREIGN KEY (user_id, job_id)
        REFERENCES genesis_import_jobs (user_id, job_id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS genesis_outputs_job_idx
    ON genesis_import_outputs (user_id, job_id);
"""

_DROP = """
DROP TABLE IF EXISTS genesis_import_outputs;
DROP TABLE IF EXISTS genesis_import_chunks;
DROP TABLE IF EXISTS genesis_import_jobs;
"""


def upgrade() -> None:
    op.execute(_DDL)


def downgrade() -> None:
    op.execute(_DROP)
