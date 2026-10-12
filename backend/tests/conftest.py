"""Shared fixtures for backend tests.

OIDC test helper
----------------
The ``oidc_test_keys`` session fixture generates an RSA-2048 key pair once per
pytest run and exposes it as a dict::

    {
        "private": <cryptography RSAPrivateKey>,
        "jwk":     <authlib JWK with kid="test-key-1">,
        "jwks":    {"keys": [<public JWK dict>]},
    }

Individual OIDC test functions use this to sign ID tokens and mock the JWKS
endpoint. The discovery + JWKS caches in ``vibony_backend.auth.oidc`` are cleared
per test via the ``clear_oidc_cache`` fixture in ``test_auth_oidc.py``.

Postgres is started as a local process via pytest-postgresql (no Docker).
Requires pg_ctl/initdb in PATH or set PGCTL env var to the pg_ctl binary.

  macOS (Homebrew):
    brew install postgresql@16
    export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"
  Or per-run:
    PGCTL=/opt/homebrew/opt/postgresql@16/bin/pg_ctl poetry run pytest
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from pytest_postgresql import factories
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

BACKEND_ROOT = Path(__file__).resolve().parent.parent

# Set required auth env vars before any test module is imported.
os.environ.setdefault("VIBONY_SESSION_SECRET", "test-secret-key-for-pytest-only-64-hex-chars-padding-here-00")
os.environ.setdefault("VIBONY_AUTH_MODE", "password")
os.environ.setdefault("VIBONY_COOKIE_SECURE", "false")  # ASGI test transport is plain HTTP
# Pin the default model so the model allowlist is deterministic in tests
# (the suite's fixtures use deepseek/deepseek-chat) regardless of a dev's .env.
# setdefault-before-import, same discipline as the sandbox vars below.
os.environ.setdefault("VIBONY_DEFAULT_MODEL", "deepseek/deepseek-chat")
# Allowlist the fallback model the fixtures pass in ai_config.fallback_models so
# session-create stays valid now that fallbacks are validated too. The
# allowlist is deny-by-default (just DEFAULT_MODEL otherwise), so a non-allowlisted
# primary OR fallback still 422s — the dedicated tests rely on that.
os.environ.setdefault("VIBONY_MODEL_ALLOWLIST", "anthropic/claude-haiku-4-5")

# Pin the sandbox provider to the in-process local backend for the whole suite.
# Set here — before the first `vibony_backend.main` import triggers load_dotenv()
# — so a dev's backend/.env (VIBONY_SANDBOX_PROVIDER=docker, used to run the
# stack) cannot leak in and make unit tests spin real Docker containers
# (non-deterministic + minutes-slow). load_dotenv(override=False) then can't win
# over this. Real-Docker coverage is the `-m docker` tests, which construct the
# provider directly and ignore this var; a deliberate shell `export` still opts in.
os.environ.setdefault("VIBONY_SANDBOX_PROVIDER", "local")
# Local mode is fail-closed in prod; the suite runs local mode, so
# explicitly opt in here (same setdefault-before-import discipline as above).
os.environ.setdefault("VIBONY_ALLOW_LOCAL_SANDBOX", "1")
# The session-less /v1/chat relay is gated off by default in prod;
# the smoke tests exercise it, so opt in here. A dedicated test unsets it.
os.environ.setdefault("VIBONY_ALLOW_UNAUTH_CHAT", "1")
# Disable rate limiting for the suite — tests fire many requests from one
# client and would otherwise hit 429. Dedicated tests flip it on via monkeypatch.
os.environ.setdefault("VIBONY_RATELIMIT_ENABLED", "0")

# Local Postgres process — session-scoped, started once per pytest run.
# Name is intentionally distinct from the built-in pytest-postgresql fixture.
postgres_proc = factories.postgresql_proc(
    executable=os.environ.get("PGCTL"),  # None → auto-detect from PATH
    dbname="vibony_test",
)


def _alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option(
        "script_location", str(BACKEND_ROOT / "vibony_backend" / "db" / "migrations")
    )
    return cfg


@pytest.fixture(scope="session")
def database_url(postgres_proc) -> str:  # type: ignore[name-defined]
    import psycopg

    # The proc fixture starts the cluster but doesn't create our database.
    # Connect to the default "postgres" DB and create it.
    with psycopg.connect(
        host=postgres_proc.host,
        port=postgres_proc.port,
        user=postgres_proc.user,
        dbname="postgres",
        autocommit=True,
    ) as conn:
        conn.execute(f'CREATE DATABASE "{postgres_proc.dbname}"')

    url = (
        f"postgresql+asyncpg://{postgres_proc.user}@"
        f"{postgres_proc.host}:{postgres_proc.port}/{postgres_proc.dbname}"
    )
    os.environ["VIBONY_DATABASE_URL"] = url
    return url


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def db_engine(database_url: str) -> AsyncIterator:
    """Apply Alembic migrations and return AsyncEngine."""

    command.upgrade(_alembic_config(database_url), "head")
    engine = create_async_engine(database_url, future=True)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def db_session(db_engine) -> AsyncIterator[AsyncSession]:
    """Clean transaction per test — everything is rolled back at the end."""

    sm = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    async with db_engine.connect() as conn:
        trans = await conn.begin()
        async with sm(bind=conn) as session:
            yield session
        # IntegrityError tests may already have rolled back this transaction.
        if trans.is_active:
            await trans.rollback()


@pytest_asyncio.fixture(loop_scope="session")
async def http_client(db_engine) -> AsyncIterator:
    """ASGI httpx client for REST tests.

    Overrides session_scope to use the test engine. Truncates all tables
    before each test for isolation (REST handlers do real commits).
    """

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import text

    from vibony_backend.db.engine import session_scope as real_session_scope
    from vibony_backend.main import app

    sm = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)

    async def override_session_scope() -> AsyncIterator[AsyncSession]:
        async with sm() as session:
            yield session

    app.dependency_overrides[real_session_scope] = override_session_scope

    async with db_engine.begin() as conn:
        await conn.execute(text(
            "TRUNCATE TABLE audit_log, session_assessments, events, sessions, task_favorites, "
            "tasks, memberships, oauth_identities, invites, users, companies RESTART IDENTITY CASCADE"
        ))

    # Seed the reference tasks as templates — the app lifespan seed doesn't
    # run under ASGITransport, but session-create / bundle REST now resolve the task
    # from the DB, so REST tests need the templates present (mirrors production).
    from vibony_backend.tasks.seed import seed_template_tasks

    async with sm() as seed_session:
        async with seed_session.begin():
            await seed_template_tasks(seed_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    app.dependency_overrides.pop(real_session_scope, None)


@pytest_asyncio.fixture(loop_scope="session")
async def authed_client(http_client) -> AsyncIterator:
    """http_client that has already signed up — yields (client, company_id_str).

    Depends on http_client (which truncates tables before each test), so each
    test starts with a fresh DB state and a fresh user+company.
    """
    resp = await http_client.post("/auth/signup", json={
        "email": "authed_test@example.com",
        "password": "password123",
        "company_name": "Test Co",
    })
    assert resp.status_code == 201, resp.text
    company_id = resp.json()["company"]["id"]
    yield http_client, company_id


@pytest.fixture(scope="session")
def oidc_test_keys() -> dict:
    """Session-scoped RSA-2048 key pair for OIDC ID token signing in tests."""
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from authlib.jose import JsonWebKey

    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    jwk = JsonWebKey.import_key(pem, {"kid": "test-key-1", "alg": "RS256"})
    return {
        "private": private_key,
        "jwk": jwk,
        "jwks": {"keys": [jwk.as_dict()]},
    }


@pytest.fixture
def isolated_agent_storage(monkeypatch):
    """Opt-in storage doubles for host unit tests; integration tests use real DB rows."""
    from unittest.mock import AsyncMock

    for name, value in {
        "load_runtime": None,
        "save_runtime": None,
        "load_agent_messages": None,
        "replay_messages_for_agent": [],
        "has_assistant_message": False,
        "save_agent_messages": None,
    }.items():
        monkeypatch.setattr(f"vibony_backend.agent_host.host.{name}", AsyncMock(return_value=value))
