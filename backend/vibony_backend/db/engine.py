"""Async engine + session factory.

URL is read from `VIBONY_DATABASE_URL` (see backend/.env.example). Connecting
via asyncpg requires the `postgresql+asyncpg://` prefix.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from types import TracebackType

from anyio import CancelScope
from sqlalchemy.dialects import registry
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


class _CancellationSafeAsyncpgDialect(PGDialect_asyncpg):
    supports_statement_cache = True

    def do_terminate(self, dbapi_connection) -> None:
        # Garbage collection can terminate a connection after its loop is gone.
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        if task is None:
            super().do_terminate(dbapi_connection)
            return
        # SQLAlchemy also terminates connections during failed queries and pool
        # pre-ping, before an AsyncSession owns them. Shield this cleanup from
        # ASGI cancellation too; normal queries remain cancellable.
        with CancelScope(shield=True):
            super().do_terminate(dbapi_connection)


registry.register(
    "postgresql.vibony_asyncpg", __name__, "_CancellationSafeAsyncpgDialect"
)


class _CancellationSafeSession(AsyncSession):
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # ASGI task groups may cancel at every await. Finish rollback/close before
        # returning, rather than leaving SQLAlchemy's shielded cleanup task behind
        # when the request's event loop exits. Queries remain cancellable.
        with CancelScope(shield=True):
            await super().__aexit__(exc_type, exc_value, traceback)


def get_database_url() -> str:
    url = os.environ.get("VIBONY_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "VIBONY_DATABASE_URL is not set. See backend/.env.example."
        )
    return url


def init_engine(url: str | None = None) -> AsyncEngine:
    """Create (or recreate) the global engine and sessionmaker."""

    global _engine, _sessionmaker
    if _engine is not None:
        return _engine
    resolved = make_url(url or get_database_url())
    if resolved.drivername == "postgresql+asyncpg":
        resolved = resolved.set(drivername="postgresql+vibony_asyncpg")
    _engine = create_async_engine(resolved, future=True, pool_pre_ping=True)
    _sessionmaker = async_sessionmaker(
        _engine, expire_on_commit=False, class_=_CancellationSafeSession
    )
    return _engine


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        init_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def session_scope() -> AsyncIterator[AsyncSession]:
    """Async generator for FastAPI Depends."""

    sm = get_sessionmaker()
    async with sm() as session:
        yield session
