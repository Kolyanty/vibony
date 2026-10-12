"""Assessment job runner + trigger + startup recovery.

In-process ``asyncio`` task, no queue/broker — mirrors ``async_timer``:

* :func:`enqueue` is called on the completion transition of **both** session
  modes: the async terminal path (``async_timer.end_async_session``) and the
  live interviewer/candidate ``POST /end`` (``rest.end_session``). It INSERTs a
  ``pending`` ``session_assessments`` row ``ON CONFLICT DO NOTHING`` and spawns the
  job **only when it actually inserted** — so a duplicate completion never
  re-runs the LLM.
* :func:`_run` claims the work with an ``UPDATE pending → running`` (single
  winner), assembles inputs, generates the report, and writes
  ``ready`` (summary/findings/model) or ``failed`` (error). A failed job leaves a
  ``failed`` row the employer can regenerate; it never blocks session completion.
* :func:`regenerate` resets the row to ``pending`` (clearing the old output) and
  re-spawns — the regenerate endpoint's backing call.
* :func:`recover_pending_assessments` re-runs rows stuck in ``pending``/``running``
  at lifespan startup (a restart loses the in-process task). True for the current
  single-uvicorn-worker deploy; a real queue is post-PMF.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from vibony_backend.llm_usage import session_usage
from vibony_backend.sessions.runtime_store import load_runtime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from vibony_backend.assessment.generate import generate_assessment
from vibony_backend.assessment.inputs import (
    TaskSpec,
    assemble_assessment_inputs,
    parse_task_spec,
)
from vibony_backend.db.engine import get_sessionmaker
from vibony_backend.db.models import Session as DbSession
from vibony_backend.db.models import SessionAssessment
from vibony_backend.tasks.store import resolve_for_session

logger = logging.getLogger(__name__)

# Cap the stored failure detail so a giant traceback can't bloat the row.
_ERROR_CHAR_CAP = 4000


async def _insert_pending(session_id: uuid.UUID) -> bool:
    """INSERT a ``pending`` row; return ``True`` iff a fresh row was inserted.

    ``ON CONFLICT (session_id) DO NOTHING`` — a session that already has an
    assessment (any status) is left untouched and ``False`` is returned, so the
    completion path never clobbers an existing report.

    A ``ForeignKeyViolation`` (the session was hard-deleted by an erasure
    between completion and enqueue) is swallowed → ``False``: there is
    nothing to assess, and :func:`enqueue` must never propagate into its callers'
    completion path.
    """
    sm = get_sessionmaker()
    try:
        async with sm() as db:
            async with db.begin():
                stmt = (
                    pg_insert(SessionAssessment)
                    .values(session_id=session_id, status="pending")
                    .on_conflict_do_nothing(index_elements=["session_id"])
                    .returning(SessionAssessment.id)
                )
                result = await db.execute(stmt)
                return result.scalar_one_or_none() is not None
    except IntegrityError:
        logger.info(
            "assessment enqueue: session %s gone (FK violation) — nothing to "
            "assess, skipping",
            session_id,
        )
        return False


async def _claim_running(session_id: uuid.UUID) -> bool:
    """Atomically move ``pending → running``; return ``True`` iff this call won.

    The single-winner guard: an enqueue racing the startup recovery cannot
    both run the LLM, because only the first ``pending → running`` UPDATE matches.
    """
    sm = get_sessionmaker()
    async with sm() as db:
        async with db.begin():
            result = await db.execute(
                update(SessionAssessment)
                .where(
                    SessionAssessment.session_id == session_id,
                    SessionAssessment.status == "pending",
                )
                .values(status="running", updated_at=func.now())
            )
            return result.rowcount == 1


async def _load_task_for_assessment(
    session_id: uuid.UUID,
) -> tuple[str, TaskSpec | None] | None:
    """Resolve a session's task slug + parsed assessment spec from the DB.

    Returns ``None`` only when the session row is gone. The spec is ``None`` when
    the task can't be resolved (deleted) — assessment still runs (near-empty/honest).
    """
    sm = get_sessionmaker()
    async with sm() as db:
        session = await db.get(DbSession, session_id)
        if session is None:
            return None
        loaded = await resolve_for_session(db, session)
        spec = (
            parse_task_spec(session.task_id, loaded.spec)
            if loaded is not None
            else None
        )
        return session.task_id, spec


async def _finalize_ready(
    session_id: uuid.UUID, summary_md: str, findings: list[dict], model: str
) -> None:
    """Write a ``ready`` result, but only for the row this job left ``running``.

    Status-scoped: if a ``regenerate`` reset the row to
    ``pending`` (and re-spawned) or another run already finalized it, ``rowcount``
    is ``0`` and this now-stale result is dropped instead of clobbering the live
    row back to old output.
    """
    sm = get_sessionmaker()
    async with sm() as db:
        async with db.begin():
            result = await db.execute(
                update(SessionAssessment)
                .where(
                    SessionAssessment.session_id == session_id,
                    SessionAssessment.status == "running",
                )
                .values(
                    status="ready",
                    summary_md=summary_md,
                    findings=findings,
                    model=model,
                    error=None,
                    updated_at=func.now(),
                )
            )
            rowcount = result.rowcount
    if rowcount == 0:
        logger.info(
            "assessment _finalize_ready: row for session %s no longer 'running' "
            "(superseded by regenerate/recovery) — result dropped",
            session_id,
        )


async def _finalize_failed(session_id: uuid.UUID, error: str) -> None:
    """Record a ``failed`` result, scoped to the row this job left ``running``.

    Same status guard as :func:`_finalize_ready` — a superseded run's failure must
    not clobber a fresh ``pending``/``running`` row from a regenerate.
    """
    sm = get_sessionmaker()
    async with sm() as db:
        async with db.begin():
            result = await db.execute(
                update(SessionAssessment)
                .where(
                    SessionAssessment.session_id == session_id,
                    SessionAssessment.status == "running",
                )
                .values(
                    status="failed",
                    error=error[:_ERROR_CHAR_CAP],
                    updated_at=func.now(),
                )
            )
            rowcount = result.rowcount
    if rowcount == 0:
        logger.info(
            "assessment _finalize_failed: row for session %s no longer 'running' "
            "(superseded) — failure not recorded",
            session_id,
        )


async def _run(session_id: uuid.UUID, session_code: str) -> None:
    """Claim + generate one assessment. Best-effort: never raises."""
    if not await _claim_running(session_id):
        logger.info(
            "assessment _run: not claimed (already running/done or gone) code=%s",
            session_code,
        )
        return

    try:
        state = await load_runtime(session_code) or {}
        controller = state.get("controller", {})
        if (state.get("turn_in_progress") or state.get("fs_write_in_progress") or state.get("integrity_errors")
                or controller.get("fail_opens") or controller.get("unresolved_rounds")):
            raise RuntimeError("Session evidence is incomplete or controller checks failed; assessment withheld")
        resolved = await _load_task_for_assessment(session_id)
        if resolved is None:
            raise RuntimeError(f"session {session_code} not found")
        task_id, spec = resolved
        inputs = await assemble_assessment_inputs(session_code, task_id, spec=spec)
        with session_usage(session_code):
            summary_md, findings, model = await generate_assessment(inputs)
        await _finalize_ready(session_id, summary_md, findings, model)
        logger.info(
            "assessment ready: code=%s model=%s findings=%d",
            session_code,
            model,
            len(findings),
        )
    except Exception as exc:
        logger.exception("assessment failed: code=%s", session_code)
        try:
            await _finalize_failed(session_id, str(exc))
        except Exception:
            logger.exception(
                "assessment: could not record failure for code=%s", session_code
            )


class AssessmentJobRegistry:
    """In-process map of session_code → running assessment task (mirror of
    :class:`~vibony_backend.sessions.async_timer.AsyncTimerRegistry`)."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    async def spawn(
        self, session_id: uuid.UUID, session_code: str
    ) -> asyncio.Task[None]:
        """Spawn :func:`_run` for a session if one isn't already running.

        Safe to call repeatedly — a live task is reused. Returns the (existing or
        fresh) task so callers/tests can await it.
        """
        async with self._lock:
            existing = self._tasks.get(session_code)
            if existing is not None and not existing.done():
                return existing
            task = asyncio.create_task(self._run_guarded(session_id, session_code))
            self._tasks[session_code] = task
            return task

    async def cancel(self, session_code: str) -> None:
        """Cancel + forget any in-flight job for a session, awaiting it to settle.

        Used by :func:`regenerate`: without this, :meth:`spawn` would
        reuse the still-running stale task — whose finalize would clobber the reset
        row back to the old output. Cancelling first means :meth:`spawn` creates a
        fresh run, and the cancelled run never reaches its (status-guarded)
        finalize. A no-op if nothing is in flight.
        """
        async with self._lock:
            task = self._tasks.pop(session_code, None)
        if task is not None and not task.done():
            task.cancel()
            # gather(return_exceptions=True) absorbs the CancelledError the child
            # raises; only a cancellation of *this* coroutine would propagate.
            await asyncio.gather(task, return_exceptions=True)

    async def _run_guarded(
        self, session_id: uuid.UUID, session_code: str
    ) -> None:
        try:
            await _run(session_id, session_code)
        finally:
            if self._tasks.get(session_code) is asyncio.current_task():
                self._tasks.pop(session_code, None)


# Global singleton — mirror of async_timer_registry / agent_host_registry.
assessment_job_registry = AssessmentJobRegistry()


async def enqueue(
    session_id: uuid.UUID, session_code: str
) -> asyncio.Task[None] | None:
    """Best-effort: insert a ``pending`` row and spawn the job iff fresh.

    Returns the spawned task (so a caller/test can await it), or ``None`` when the
    session already had an assessment (no re-run). Called from both completion
    paths; never raises — a failed enqueue must not block the session ending.
    """
    if not await _insert_pending(session_id):
        return None
    return await assessment_job_registry.spawn(session_id, session_code)


async def regenerate(
    session_id: uuid.UUID, session_code: str
) -> asyncio.Task[None]:
    """Reset the assessment to ``pending`` (clearing prior output) and re-spawn.

    Backs ``POST /sessions/{code}/assessment/regenerate``. Creates the row
    if none exists yet. Returns the spawned task.

    Cancels any in-flight run first so the reset+re-spawn below is not
    short-circuited by :meth:`spawn` handing back the stale task.
    """
    await assessment_job_registry.cancel(session_code)
    sm = get_sessionmaker()
    async with sm() as db:
        async with db.begin():
            result = await db.execute(
                update(SessionAssessment)
                .where(SessionAssessment.session_id == session_id)
                .values(
                    status="pending",
                    summary_md=None,
                    findings=[],
                    model=None,
                    error=None,
                    updated_at=func.now(),
                )
            )
            if result.rowcount == 0:
                await db.execute(
                    pg_insert(SessionAssessment)
                    .values(session_id=session_id, status="pending")
                    .on_conflict_do_nothing(index_elements=["session_id"])
                )
    return await assessment_job_registry.spawn(session_id, session_code)


async def recover_pending_assessments() -> int:
    """Re-run rows stuck in ``pending``/``running`` at backend startup.

    A restart loses the in-process task. A row left ``running`` is stranded (its
    process died mid-run); since startup means no live task exists, we reset such
    rows back to ``pending`` so :func:`_claim_running` can win, then spawn. Returns
    the count re-armed.
    """
    sm = get_sessionmaker()
    async with sm() as db:
        async with db.begin():
            # Stranded 'running' rows → 'pending' so the claim can re-win. This
            # reset is GLOBAL (every 'running' row), which is correct only because
            # this runs at lifespan startup on a single uvicorn worker: no
            # job task is live yet, so nothing legitimate is mid-flight to steal.
            # A multi-worker deploy (post-PMF) would need a per-row lease/TTL.
            await db.execute(
                update(SessionAssessment)
                .where(SessionAssessment.status == "running")
                .values(status="pending", updated_at=func.now())
            )
            rows = (
                await db.execute(
                    select(SessionAssessment.session_id, DbSession.code)
                    .join(DbSession, SessionAssessment.session_id == DbSession.id)
                    .where(SessionAssessment.status == "pending")
                )
            ).all()

    for session_id, code in rows:
        await assessment_job_registry.spawn(session_id, code)
    if rows:
        logger.info(
            "recover_pending_assessments: re-armed %d pending assessment(s)",
            len(rows),
        )
    return len(rows)


__all__ = [
    "AssessmentJobRegistry",
    "assessment_job_registry",
    "enqueue",
    "recover_pending_assessments",
    "regenerate",
]
