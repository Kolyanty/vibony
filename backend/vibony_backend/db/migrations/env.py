"""Alembic env: synchronous engine (psycopg).

The URL comes from `VIBONY_DATABASE_URL` (or from `sqlalchemy.url` in alembic.ini).
If the URL contains `+asyncpg`, it is replaced with `+psycopg` so Alembic runs
in synchronous mode. The async engine is used only in production code
(`vibony_backend/db/engine.py`); migrations are always sync so they can be run
both from the CLI (`alembic upgrade head`) and from pytest fixtures
without an event-loop clash.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from vibony_backend.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_url() -> str:
    url = os.environ.get("VIBONY_DATABASE_URL") or config.get_main_option(
        "sqlalchemy.url"
    )
    if not url:
        raise RuntimeError(
            "VIBONY_DATABASE_URL is not set and there is no sqlalchemy.url in alembic.ini"
        )
    return url.replace("+asyncpg", "+psycopg")


config.set_main_option("sqlalchemy.url", _resolve_url())

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section, {})
    connectable = engine_from_config(
        cfg, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
