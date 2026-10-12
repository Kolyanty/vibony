"""Assessment job runner + trigger + recovery.

``generate_assessment`` is mocked everywhere — no real LLM call. Covers (against
the real Postgres fixture; the job opens its own session via the module
sessionmaker, same pattern as test_async_timer's ``timer_db``):

  - enqueue inserts + runs to ``ready``; a second enqueue is a no-op (exactly-once).
  - the ``pending → running`` claim has a single winner.
  - a failed generation → ``failed`` row, the session stays ``completed``.
  - regenerate resets a ready row and re-runs.
  - recovery re-runs a stranded ``running`` row.
  - the trigger wiring: ``end_async_session`` calls ``enqueue``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vibony_backend.assessment.job import (
    _claim_running,
    _finalize_ready,
    _insert_pending,
    assessment_job_registry,
    enqueue,
    recover_pending_assessments,
    regenerate,
)
from vibony_backend.db.models import Event
from vibony_backend.db.models import Session as DbSession
from vibony_backend.db.models import SessionAssessment

pytestmark = pytest.mark.asyncio(loop_scope="session")

GEN_RESULT = (
    "## What went well\nok\n## What's missing for an ideal solution\n-\n## Observed reasoning patterns\n-",
    [{"category": "strength", "title": "t", "detail": "d", "severity": None, "event_seq": None}],
    "test-model",
)
STALE_RESULT = ("## stale summary", [], "stale-model")


@pytest_asyncio.fixture(loop_scope="session")
async def job_db(db_engine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Point the module sessionmaker at the test engine; truncate around."""
    import vibony_backend.db.engine as eng

    async def _truncate() -> None:
        async with db_engine.begin() as conn:
            await conn.execute(text(
                "TRUNCATE TABLE session_assessments, events, sessions, memberships, "
                "oauth_identities, invites, users, companies RESTART IDENTITY CASCADE"
            ))

    await _truncate()
    eng._engine = db_engine
    eng._sessionmaker = async_sessionmaker(
        db_engine, expire_on_commit=False, class_=AsyncSession
    )
    yield eng._sessionmaker
    await _truncate()
    eng._engine = None
    eng._sessionmaker = None


async def _seed_session(
    sm: async_sessionmaker[AsyncSession],
    code: str,
    *,
    status: str = "completed",
    mode: str = "async",
    with_session_start: bool = False,
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
                time_limit_seconds=3600 if mode == "async" else None,
            )
        )
        if with_session_start:
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


async def _get_assessment(
    sm: async_sessionmaker[AsyncSession], session_id: uuid.UUID
) -> SessionAssessment | None:
    async with sm() as db:
        return (
            await db.execute(
                select(SessionAssessment).where(
                    SessionAssessment.session_id == session_id
                )
            )
        ).scalar_one_or_none()


async def _session_status(
    sm: async_sessionmaker[AsyncSession], session_id: uuid.UUID
) -> str:
    async with sm() as db:
        return (
            await db.execute(
                select(DbSession.status).where(DbSession.id == session_id)
            )
        ).scalar_one()


# ---------------------------------------------------------------------------
# enqueue + _run
# ---------------------------------------------------------------------------


async def test_enqueue_inserts_and_runs_to_ready(job_db):
    sid = await _seed_session(job_db, "JOB-0001")
    with patch(
        "vibony_backend.assessment.job.generate_assessment",
        new=AsyncMock(return_value=GEN_RESULT),
    ):
        task = await enqueue(sid, "JOB-0001")
        assert task is not None
        await task
    row = await _get_assessment(job_db, sid)
    assert row is not None
    assert row.status == "ready"
    assert row.model == "test-model"
    assert len(row.findings) == 1
    assert row.summary_md.startswith("## What went well")
    assert row.error is None


async def test_enqueue_exactly_once(job_db):
    sid = await _seed_session(job_db, "JOB-0002")
    gen = AsyncMock(return_value=GEN_RESULT)
    with patch("vibony_backend.assessment.job.generate_assessment", new=gen):
        t1 = await enqueue(sid, "JOB-0002")
        assert t1 is not None
        await t1
        # Row now exists (ready) → second enqueue inserts nothing, spawns nothing.
        t2 = await enqueue(sid, "JOB-0002")
        assert t2 is None
    assert gen.await_count == 1


async def test_claim_running_single_winner(job_db):
    sid = await _seed_session(job_db, "JOB-0003")
    assert await _insert_pending(sid) is True
    # Second insert is a no-op (conflict).
    assert await _insert_pending(sid) is False
    assert await _claim_running(sid) is True
    # Already running → the second claim loses.
    assert await _claim_running(sid) is False


async def test_failed_generation_marks_failed_session_stays_completed(job_db):
    sid = await _seed_session(job_db, "JOB-0004", status="completed")
    boom = AsyncMock(side_effect=RuntimeError("llm down"))
    with patch("vibony_backend.assessment.job.generate_assessment", new=boom):
        task = await enqueue(sid, "JOB-0004")
        await task
    row = await _get_assessment(job_db, sid)
    assert row.status == "failed"
    assert "llm down" in row.error
    # The job failure never touched the session itself.
    assert await _session_status(job_db, sid) == "completed"


async def test_regenerate_resets_ready_row_and_reruns(job_db):
    sid = await _seed_session(job_db, "JOB-0006")
    async with job_db() as db:
        db.add(
            SessionAssessment(
                session_id=sid,
                status="ready",
                model="old-model",
                summary_md="old summary",
                findings=[{"category": "gap", "title": "old", "detail": "d"}],
            )
        )
        await db.commit()

    gen = AsyncMock(return_value=GEN_RESULT)
    with patch("vibony_backend.assessment.job.generate_assessment", new=gen):
        task = await regenerate(sid, "JOB-0006")
        await task
    row = await _get_assessment(job_db, sid)
    assert row.status == "ready"
    assert row.model == "test-model"
    assert row.summary_md.startswith("## What went well")
    assert gen.await_count == 1


# ---------------------------------------------------------------------------
# regenerate / finalize correctness
# ---------------------------------------------------------------------------


async def test_regenerate_cancels_inflight_and_keeps_new_result(job_db):
    """Regenerate cancels the still-running stale job (so spawn does not
    reuse it) and the fresh run's result wins — the reset row is not clobbered back
    to the old output."""
    sid = await _seed_session(job_db, "JOB-RGN1")
    calls: list[int] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fake_generate(inputs, model=None):
        calls.append(1)
        if len(calls) == 1:
            entered.set()  # first (stale) run reached generate → row is 'running'
            await release.wait()  # block until cancelled
            return STALE_RESULT
        return GEN_RESULT  # fresh run

    with patch(
        "vibony_backend.assessment.job.generate_assessment", new=fake_generate
    ):
        first = await enqueue(sid, "JOB-RGN1")
        assert first is not None
        await asyncio.wait_for(entered.wait(), timeout=5)
        # regenerate must cancel `first` and spawn a fresh run.
        second = await regenerate(sid, "JOB-RGN1")
        await asyncio.wait_for(second, timeout=5)

    release.set()  # unblock the (already cancelled) first run — harmless
    row = await _get_assessment(job_db, sid)
    assert row.status == "ready"
    assert row.model == "test-model"  # GEN_RESULT, not STALE_RESULT
    assert row.summary_md.startswith("## What went well")
    assert first.cancelled()


async def test_finalize_ready_is_status_scoped(job_db):
    """A result is only written for a row still in 'running'. A row that
    was reset (e.g. regenerate → 'pending') is left untouched, so a superseded run
    cannot clobber it."""
    sid = await _seed_session(job_db, "JOB-FIN1")
    await _insert_pending(sid)  # row is 'pending', not the 'running' a job leaves

    await _finalize_ready(sid, "stale summary", [], "stale-model")

    row = await _get_assessment(job_db, sid)
    assert row.status == "pending"  # guard held — write dropped
    assert row.summary_md is None
    assert row.model is None


async def test_enqueue_on_deleted_session_is_noop(job_db):
    """A session hard-deleted (erasure) between completion and
    enqueue → the pending INSERT hits an FK violation → enqueue swallows it and
    returns None instead of propagating into the completion path."""
    sid = uuid.uuid4()  # never inserted → FK violation on the pending INSERT
    gen = AsyncMock(return_value=GEN_RESULT)
    with patch("vibony_backend.assessment.job.generate_assessment", new=gen):
        task = await enqueue(sid, "GONE-0001")
    assert task is None
    assert gen.await_count == 0
    assert await _get_assessment(job_db, sid) is None


# ---------------------------------------------------------------------------
# recovery
# ---------------------------------------------------------------------------


async def test_recover_reruns_stranded_running(job_db):
    sid = await _seed_session(job_db, "JOB-0005")
    await _insert_pending(sid)
    await _claim_running(sid)  # leave it stranded in 'running'

    gen = AsyncMock(return_value=GEN_RESULT)
    with patch("vibony_backend.assessment.job.generate_assessment", new=gen):
        n = await recover_pending_assessments()
        assert n == 1
        task = assessment_job_registry._tasks.get("JOB-0005")
        if task is not None:
            await task
    row = await _get_assessment(job_db, sid)
    assert row.status == "ready"
    assert gen.await_count == 1


# ---------------------------------------------------------------------------
# trigger wiring: end_async_session → enqueue
# ---------------------------------------------------------------------------


async def test_end_async_session_enqueues(job_db):
    from vibony_backend.agent_host.host import agent_host_registry
    from vibony_backend.sessions.async_timer import end_async_session

    sid = await _seed_session(
        job_db, "JOB-WIRE", status="active", with_session_start=True
    )
    fake = MagicMock()
    fake.terminate = AsyncMock(return_value=None)
    fake.quiesce = AsyncMock(return_value=None)  # quiesce drain
    agent_host_registry._hosts["JOB-WIRE"] = fake

    spy = AsyncMock(return_value=None)
    try:
        with patch("vibony_backend.assessment.job.enqueue", new=spy):
            did = await end_async_session(sid, "JOB-WIRE", "submitted")
    finally:
        agent_host_registry._hosts.pop("JOB-WIRE", None)

    assert did is True
    spy.assert_awaited_once_with(sid, "JOB-WIRE")
