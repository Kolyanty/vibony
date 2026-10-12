"""Async session lifecycle (server-side timer + terminal transition).

Covers, against the real Postgres fixture (the functions open their own session
via the module sessionmaker, same pattern as test_agent_host_blob_persist):

  - mark_async_started anchors started_at once, idempotent on reconnect.
  - end_async_session (submit path): emits assessment_pending + session_end,
    transitions active → completed, tears down the host, returns True; a second
    call is a no-op (idempotent) with no extra events.
  - end_async_session no-ops on a non-active session.
  - AsyncTimerRegistry: a passed deadline fires the timeout terminal transition;
    a live deadline publishes at least one advisory timer_tick (never persisted)
    before firing.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vibony_backend.db.models import Event
from vibony_backend.db.models import Session as DbSession
from vibony_backend.sessions.async_timer import (
    AsyncTimerRegistry,
    async_timer_registry,
    end_async_session,
    recover_async_timers,
)
from vibony_backend.sessions.broker import broker
from vibony_backend.sessions.event_store import mark_async_started, store_event

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture(autouse=True)
def _no_assessment_enqueue():
    """``end_async_session`` enqueues the assessment job, which would
    make a real LLM call. Stub the enqueue to a no-op so these timer tests stay
    LLM-free + deterministic; the job itself is covered by test_assessment_job.py.
    Patched in ``assessment.job`` because async_timer imports it lazily by name."""
    with patch(
        "vibony_backend.assessment.job.enqueue", new=AsyncMock(return_value=None)
    ):
        yield


@pytest_asyncio.fixture(loop_scope="session")
async def timer_db(db_engine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Truncate tables, point the module sessionmaker at the test engine, yield it."""
    import vibony_backend.db.engine as eng

    async with db_engine.begin() as conn:
        await conn.execute(text(
            "TRUNCATE TABLE session_assessments, events, sessions, memberships, "
            "oauth_identities, invites, users, companies RESTART IDENTITY CASCADE"
        ))

    eng._engine = db_engine
    eng._sessionmaker = async_sessionmaker(
        db_engine, expire_on_commit=False, class_=AsyncSession
    )

    yield eng._sessionmaker

    async with db_engine.begin() as conn:
        await conn.execute(text(
            "TRUNCATE TABLE session_assessments, events, sessions, memberships, "
            "oauth_identities, invites, users, companies RESTART IDENTITY CASCADE"
        ))

    eng._engine = None
    eng._sessionmaker = None


async def _seed_async_session(
    sm: async_sessionmaker[AsyncSession],
    code: str,
    *,
    status: str = "active",
    time_limit_seconds: int | None = 3600,
    started_at: datetime | None = None,
    mode: str = "async",
    with_session_start: bool = True,
) -> uuid.UUID:
    sid = uuid.uuid4()
    async with sm() as db:
        db.add(
            DbSession(
                id=sid,
                code=code,
                status=status,
                task_id="task-01-training-loop",
                protocol_version=7,
                ai_config={"primary_model": "test"},
                candidate_token=str(uuid.uuid4()),
                expires_at=datetime.now(UTC) + timedelta(hours=8),
                mode=mode,
                time_limit_seconds=time_limit_seconds,
                started_at=started_at,
            )
        )
        if with_session_start:
            # store_event invariants require a session_start to precede other events.
            db.add(
                Event(
                    session_id=sid,
                    seq=1,
                    id=uuid.uuid4(),
                    type="session_start",
                    client_timestamp=datetime.now(UTC),
                    payload={"task_id": "task-01-training-loop"},
                )
            )
        await db.commit()
    return sid


async def _event_types(sm: async_sessionmaker[AsyncSession], code: str) -> list[str]:
    async with sm() as db:
        rows = (
            await db.execute(
                select(Event.type)
                .join(DbSession, Event.session_id == DbSession.id)
                .where(DbSession.code == code)
                .order_by(Event.seq)
            )
        ).all()
    return [t for (t,) in rows]


async def _status(sm: async_sessionmaker[AsyncSession], code: str) -> str:
    async with sm() as db:
        return (
            await db.execute(select(DbSession.status).where(DbSession.code == code))
        ).scalar_one()


# ---------------------------------------------------------------------------
# mark_async_started
# ---------------------------------------------------------------------------


async def test_mark_async_started_anchors_once(timer_db) -> None:
    sid = await _seed_async_session(timer_db, "TMR-0001", started_at=None)

    first = await mark_async_started(sid)
    assert first is not None

    # Reconnect: started_at is not re-anchored.
    second = await mark_async_started(sid)
    assert second == first


async def test_mark_async_started_missing_session(timer_db) -> None:
    assert await mark_async_started(uuid.uuid4()) is None


# ---------------------------------------------------------------------------
# end_async_session
# ---------------------------------------------------------------------------


async def test_end_async_session_submit_path(timer_db, monkeypatch) -> None:
    sid = await _seed_async_session(timer_db, "TMR-0002")

    fake_host = MagicMock()
    fake_host.terminate = AsyncMock(return_value=None)

    # The in-flight turn must be drained *before* session_end
    # flips the session to completed — assert the session is still active when
    # quiesce runs, proving the ordering (not just that quiesce was called).
    async def _quiesce_while_active(*_a, **_kw):
        assert await _status(timer_db, "TMR-0002") == "active"

    fake_host.quiesce = AsyncMock(side_effect=_quiesce_while_active)
    async_timer_registry._tasks.pop("TMR-0002", None)
    from vibony_backend.agent_host.host import agent_host_registry

    agent_host_registry._hosts["TMR-0002"] = fake_host
    try:
        did = await end_async_session(sid, "TMR-0002", "submitted")
    finally:
        agent_host_registry._hosts.pop("TMR-0002", None)

    assert did is True
    assert await _status(timer_db, "TMR-0002") == "completed"
    types = await _event_types(timer_db, "TMR-0002")
    # assessment_pending precedes the terminal session_end.
    assert types == ["session_start", "assessment_pending", "session_end"]
    fake_host.quiesce.assert_awaited_once()
    fake_host.terminate.assert_awaited_once()

    # completed_reason persisted from the session_end payload.
    async with timer_db() as db:
        row = (
            await db.execute(select(DbSession).where(DbSession.code == "TMR-0002"))
        ).scalar_one()
    assert row.completed_reason == "submitted"


async def test_end_async_session_idempotent(timer_db) -> None:
    sid = await _seed_async_session(timer_db, "TMR-0003")

    assert await end_async_session(sid, "TMR-0003", "submitted") is True
    # Second call: session already completed → no-op, no duplicate events.
    assert await end_async_session(sid, "TMR-0003", "timeout") is False

    types = await _event_types(timer_db, "TMR-0003")
    assert types.count("session_end") == 1
    assert types.count("assessment_pending") == 1


async def test_end_async_session_noop_when_not_active(timer_db) -> None:
    sid = await _seed_async_session(
        timer_db, "TMR-0004", status="pending", with_session_start=False
    )
    assert await end_async_session(sid, "TMR-0004", "submitted") is False
    assert await _event_types(timer_db, "TMR-0004") == []


# ---------------------------------------------------------------------------
# AsyncTimerRegistry
# ---------------------------------------------------------------------------


async def test_timer_fires_timeout_when_deadline_passed(timer_db) -> None:
    sid = await _seed_async_session(timer_db, "TMR-0005", time_limit_seconds=60)
    started_at = datetime.now(UTC) - timedelta(seconds=120)  # already past deadline

    reg = AsyncTimerRegistry()
    task = await reg.ensure_running(sid, "TMR-0005", started_at, 60)
    await task

    assert await _status(timer_db, "TMR-0005") == "completed"
    types = await _event_types(timer_db, "TMR-0005")
    assert types == ["session_start", "assessment_pending", "session_end"]

    async with timer_db() as db:
        reason = (
            await db.execute(
                select(Event.payload).join(
                    DbSession, Event.session_id == DbSession.id
                ).where(DbSession.code == "TMR-0005", Event.type == "session_end")
            )
        ).scalar_one()
    assert reason["reason"] == "timeout"


async def test_timer_publishes_tick_then_fires(timer_db) -> None:
    sid = await _seed_async_session(timer_db, "TMR-0006", time_limit_seconds=1)
    started_at = datetime.now(UTC)

    q = broker.subscribe("TMR-0006")
    reg = AsyncTimerRegistry()
    try:
        task = await reg.ensure_running(sid, "TMR-0006", started_at, 1)
        await asyncio.wait_for(task, timeout=5.0)
    finally:
        broker.unsubscribe("TMR-0006", q)

    frames = []
    while not q.empty():
        frames.append(q.get_nowait())

    tick_frames = [f for f in frames if f.get("kind") == "timer_tick"]
    assert tick_frames, "expected at least one advisory timer_tick frame"
    assert all(isinstance(f["remaining_seconds"], int) for f in tick_frames)

    # timer_tick is transient — never persisted to the event log.
    assert "timer_tick" not in await _event_types(timer_db, "TMR-0006")
    assert await _status(timer_db, "TMR-0006") == "completed"


async def test_ensure_running_reuses_live_task(timer_db) -> None:
    sid = await _seed_async_session(timer_db, "TMR-0007", time_limit_seconds=3600)
    started_at = datetime.now(UTC)

    reg = AsyncTimerRegistry()
    try:
        t1 = await reg.ensure_running(sid, "TMR-0007", started_at, 3600)
        t2 = await reg.ensure_running(sid, "TMR-0007", started_at, 3600)
        assert t1 is t2  # live task reused, not double-started
    finally:
        reg.cancel("TMR-0007")


# ---------------------------------------------------------------------------
# recover_async_timers — restart-without-reconnect
# ---------------------------------------------------------------------------
# These use the *global* async_timer_registry (recover_async_timers re-arms it).
# Each cleans up its own code in a finally so the singleton stays clean.


async def test_recover_rearms_passed_deadline_fires_timeout(timer_db) -> None:
    # An anchored async session whose deadline lapsed during downtime is driven
    # to timeout on startup even though no candidate reconnects.
    started_at = datetime.now(UTC) - timedelta(seconds=120)
    sid = await _seed_async_session(
        timer_db, "TMR-0808", time_limit_seconds=60, started_at=started_at
    )

    try:
        count = await recover_async_timers()
        assert count == 1
        task = async_timer_registry._tasks.get("TMR-0808")
        assert task is not None
        await task
    finally:
        async_timer_registry.cancel("TMR-0808")

    assert await _status(timer_db, "TMR-0808") == "completed"
    types = await _event_types(timer_db, "TMR-0808")
    assert types == ["session_start", "assessment_pending", "session_end"]
    async with timer_db() as db:
        reason = (
            await db.execute(
                select(Event.payload).join(
                    DbSession, Event.session_id == DbSession.id
                ).where(DbSession.code == "TMR-0808", Event.type == "session_end")
            )
        ).scalar_one()
    assert reason["reason"] == "timeout"


async def test_recover_keeps_in_window_session_ticking(timer_db) -> None:
    # A session still inside its window is re-armed but not yet terminal.
    sid = await _seed_async_session(
        timer_db, "TMR-0809", time_limit_seconds=3600, started_at=datetime.now(UTC)
    )

    try:
        count = await recover_async_timers()
        assert count == 1
        task = async_timer_registry._tasks.get("TMR-0809")
        assert task is not None and not task.done()
        assert await _status(timer_db, "TMR-0809") == "active"
    finally:
        async_timer_registry.cancel("TMR-0809")


async def test_recover_skips_unanchored_session(timer_db) -> None:
    # started_at is None (candidate never connected) — nothing to re-arm; the
    # timer arms on first connect as normal.
    await _seed_async_session(
        timer_db, "TMR-0810", time_limit_seconds=60, started_at=None
    )

    assert await recover_async_timers() == 0
    assert "TMR-0810" not in async_timer_registry._tasks


async def test_recover_skips_live_and_completed(timer_db) -> None:
    # Live sessions have no timer; completed sessions are already terminal.
    await _seed_async_session(
        timer_db,
        "TMR-0811",
        mode="live",
        time_limit_seconds=None,
        started_at=datetime.now(UTC),
    )
    await _seed_async_session(
        timer_db,
        "TMR-0812",
        status="completed",
        time_limit_seconds=60,
        started_at=datetime.now(UTC) - timedelta(seconds=120),
    )

    try:
        assert await recover_async_timers() == 0
    finally:
        async_timer_registry.cancel("TMR-0811")
        async_timer_registry.cancel("TMR-0812")

    assert "TMR-0811" not in async_timer_registry._tasks
    assert "TMR-0812" not in async_timer_registry._tasks


async def test_remaining_seconds_tracks_deadline() -> None:
    """remaining_seconds is None for an unarmed session, the clamped
    countdown while armed, and 0 once the deadline has passed."""
    from datetime import UTC, datetime, timedelta

    from vibony_backend.sessions.async_timer import AsyncTimerRegistry

    reg = AsyncTimerRegistry()
    assert reg.remaining_seconds("NONE-0001") is None

    reg._deadlines["ARMD-0001"] = datetime.now(UTC) + timedelta(seconds=120)
    r = reg.remaining_seconds("ARMD-0001")
    assert r is not None and 100 < r <= 120

    reg._deadlines["PAST-0001"] = datetime.now(UTC) - timedelta(seconds=5)
    assert reg.remaining_seconds("PAST-0001") == 0.0
