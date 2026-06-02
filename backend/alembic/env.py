"""Alembic migration environment.

Reads the connection string from the DATABASE_URL environment variable (the
same one the app's psycopg pool uses) and maps the bare ``postgresql://`` scheme
to the psycopg3 SQLAlchemy dialect (``postgresql+psycopg://``), since psycopg2
is not installed. Migrations are hand-written (no ORM models), so
``target_metadata`` is None and autogenerate is not used.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config

# No ORM model metadata — migrations are authored by hand.
target_metadata = None


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set — Alembic needs it to connect (must include "
            "sslmode=require for external Postgres)."
        )
    # SQLAlchemy needs an explicit driver; the app uses the bare postgresql://
    # scheme with the psycopg(3) pool. Map both schemes to the psycopg3 dialect.
    if url.startswith("postgresql+"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_database_url(), poolclass=pool.NullPool, future=True)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
