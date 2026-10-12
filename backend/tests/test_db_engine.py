"""Connection cleanup when a request is cancelled by its ASGI task group."""

from unittest.mock import Mock

import anyio
import pytest
from sqlalchemy import event, text
from sqlalchemy.util import await_only

import vibony_backend.db.engine as engine_module


def test_connection_can_be_terminated_after_event_loop_shutdown():
    connection = Mock()
    dialect = engine_module._CancellationSafeAsyncpgDialect()
    dialect.do_terminate(connection)
    connection.terminate.assert_called_once_with()


@pytest.mark.asyncio(loop_scope="session")
async def test_cancelled_session_returns_connection_before_task_group_exits(
    database_url, monkeypatch,
):
    monkeypatch.setattr(engine_module, "_engine", None)
    monkeypatch.setattr(engine_module, "_sessionmaker", None)
    engine = engine_module.init_engine(database_url)
    sessions = engine_module.get_sessionmaker()
    ready = anyio.Event()

    async def request():
        async with sessions() as session:
            await session.execute(text("SELECT 1"))
            ready.set()
            await anyio.sleep_forever()

    try:
        async with anyio.create_task_group() as group:
            group.start_soon(request)
            await ready.wait()
            group.cancel_scope.cancel()

        # Cleanup must finish before the request's event loop can be shut down.
        assert engine.sync_engine.pool.checkedout() == 0
        async with sessions() as session:
            assert await session.scalar(text("SELECT 1")) == 1
        assert engine.sync_engine.pool.checkedout() == 0
    finally:
        await engine_module.dispose_engine()


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("during_ping", [False, True], ids=["query", "pool-pre-ping"])
async def test_cancelled_query_closes_connection_without_cleanup_errors(
    database_url, monkeypatch, caplog, during_ping,
):
    monkeypatch.setattr(engine_module, "_engine", None)
    monkeypatch.setattr(engine_module, "_sessionmaker", None)
    engine = engine_module.init_engine(database_url)
    sessions = engine_module.get_sessionmaker()
    ready = anyio.Event()

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def query_started(connection, cursor, statement, parameters, context, executemany):
        if statement == "SELECT pg_sleep(30)":
            ready.set()

    async def request():
        async with sessions() as session:
            await session.execute(text("SELECT pg_sleep(30)"))

    def pending_ping(dbapi_connection):
        # Pause exactly inside pool checkout, before the session owns a connection.
        ready.set()
        return await_only(anyio.sleep_forever())

    try:
        # pre-ping only runs when reusing a connection, not on the first checkout.
        async with sessions() as session:
            await session.execute(text("SELECT 1"))
        with monkeypatch.context() as ping_patch:
            if during_ping:
                ping_patch.setattr(engine.sync_engine.dialect, "do_ping", pending_ping)
            async with anyio.create_task_group() as group:
                group.start_soon(request)
                await ready.wait()
                group.cancel_scope.cancel()

        assert engine.sync_engine.pool.checkedout() == 0
        assert not [
            record for record in caplog.records
            if record.name.startswith("sqlalchemy") and record.levelno >= 40
        ]
        async with sessions() as session:
            assert await session.scalar(text("SELECT 1")) == 1
    finally:
        await engine_module.dispose_engine()
