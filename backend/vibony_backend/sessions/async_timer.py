"""Server-side countdown timer + terminal transition for async sessions.

The backend owns the deadline (``started_at + time_limit_seconds``).
``timer_tick`` frames are advisory (UI smoothness only); the timeout fires from
this server-side task, never from the client clock, so a candidate cannot extend
their own time by stopping ticks.

Both terminal paths — the candidate's ``session_submit`` frame and the
server-side timeout — funnel through :func:`end_async_session`. It is idempotent:
the ``active → completed`` transition inside ``store_event(session_end)`` is the
single-winner claim, serialised per-code by a terminal lock so the placeholder
``assessment_pending`` event is emitted exactly once. By design the
terminal signal is the ``session_end`` event with ``reason`` ``submitted`` /
``timeout`` (reusing the existing transition + dashboard end handling) — not a
brand-new event type. ``timer_tick`` is a transient broker frame, never persisted.

The timer lives in-process and is server-authoritative: it is *not* cancelled on
candidate disconnect (the deadline is absolute). On a backend restart the
in-process task is lost. It is re-established on the next candidate connect
(:func:`AsyncTimerRegistry.ensure_running` recomputes the deadline from the
persisted ``started_at`` and fires immediately if it has already passed), and —
so a deadline still fires when the candidate never reconnects —
:func:`recover_async_timers` re-arms every already-anchored active async session
at startup.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from vibony_backend.db.engine import get_sessionmaker
from vibony_backend.db.models import Session as DbSession
from vibony_backend.sessions.broker import broker
from vibony_backend.sessions.event_store import store_event

logger = logging.getLogger(__name__)

# How often the advisory countdown frame is published during an active async
# session. The candidate UI interpolates between ticks for smoothness.
TICK_INTERVAL_SECONDS = 10.0

# Serialises the terminal transition per session_code so a submit racing the
# timeout can't double-emit the placeholder assessment_pending event. Single
# *process* only — the DB-level single-winner is store_event(session_end)'s
# active→completed claim, but assessment_pending is emitted before it, so
# exactly-once depends on this in-process lock. True today (single uvicorn
# worker); a multi-worker deploy would need a DB-level guard instead.
# The winning terminal transition pops its lock to bound dict growth.
_terminal_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


async def end_async_session(
    session_id: uuid.UUID, session_code: str, reason: str
) -> bool:
    """Drive an async session to ``completed``: ``assessment_pending`` stub +
    ``session_end(reason)``, then tear down the sandbox and cancel the timer.

    ``reason`` ∈ ``{"submitted", "timeout"}``. Idempotent — a no-op (returns
    ``False``) when the session is no longer ``active`` (already ended via the
    other terminal path or by the interviewer). Returns ``True`` when this call
    performed the transition.

    The ``assessment_pending`` event is the assessment-job hook: this step only
    records intent (no LLM call). It is emitted *before* ``session_end`` because
    the event log closes on ``session_end`` (invariant 2 — no events after it).
    Its emit is best-effort (swallowed on failure): a missing stub must not block
    the session from completing — the real assessment job can also key off completion.
    """
    lock = _terminal_locks[session_code]
    async with lock:
        sm = get_sessionmaker()
        async with sm() as db:
            status = (
                await db.execute(
                    select(DbSession.status).where(DbSession.id == session_id)
                )
            ).scalar_one_or_none()
        if status != "active":
            # Already terminal (or never started) — nothing to do.
            return False

        # Drain any in-flight agent turn before we claim the
        # session completed. Otherwise the turn's trailing events would be dropped
        # by store_event's active→completed guard, and the transcript fed to the
        # assessment could end on a dangling user_message whose turn never
        # finished. quiesce refuses new turns + awaits (or bounded-cancels) the
        # current one. Done before assessment_pending/session_end below.
        from vibony_backend.agent_host.host import agent_host_registry

        host = agent_host_registry.get(session_code)
        if host is not None:
            await host.quiesce()

        # Placeholder assessment hook — real LLM assessment lands later.
        try:
            await store_event(
                session_id, session_code, "assessment_pending", {"reason": reason}
            )
        except Exception as exc:
            logger.warning(
                "end_async_session: assessment_pending emit failed code=%s: %s",
                session_code,
                exc,
            )

        try:
            await store_event(
                session_id, session_code, "session_end", {"reason": reason}
            )
        except Exception as exc:
            # Lost the race (another path completed the session between the
            # status check and here) — leave teardown to the winner.
            logger.warning(
                "end_async_session: session_end emit failed code=%s: %s",
                session_code,
                exc,
            )
            return False

    # One-shot per session: drop the lock entry now the session is terminal, so
    # the per-code dict doesn't grow forever. A late duplicate call re-creates a
    # fresh lock, reads status=completed and returns False — no double-emit.
    _terminal_locks.pop(session_code, None)

    # Outside the lock — teardown is independently idempotent. The countdown
    # task is NOT cancelled here: on the timeout path this runs *inside* that
    # task (self-cancel is a footgun), and on the submit/REST paths the caller
    # cancels it explicitly. A stray tick after completion is harmless anyway —
    # end_async_session no-ops once the session is no longer active.
    # (agent_host_registry imported above for the quiesce step.)
    await agent_host_registry.force_terminate(session_code)

    # Enqueue the AI assessment job. Best-effort (swallowed on failure) —
    # the real report is produced by the job; a failed enqueue must not block the
    # session from completing. Idempotent (INSERT ON CONFLICT DO NOTHING), so a
    # later interviewer /end on the same session won't double-run it.
    try:
        from vibony_backend.assessment.job import enqueue

        await enqueue(session_id, session_code)
    except Exception as exc:
        logger.warning(
            "end_async_session: assessment enqueue failed code=%s: %s",
            session_code,
            exc,
        )

    logger.info(
        "async session ended: code=%s reason=%s", session_code, reason
    )
    return True


class AsyncTimerRegistry:
    """In-process map of session_code → countdown task for async sessions."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # Absolute deadline per async session, so the agent loop can cap an
        # LLM-call timeout at the remaining budget (a hung call can't run past the
        # candidate's clock). Tracked alongside the task; cleared with it.
        self._deadlines: dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    def remaining_seconds(self, session_code: str) -> float | None:
        """Seconds left on the async deadline, or None for a non-async / unarmed
        session. Clamped to ≥ 0. Lock-free read of one dict entry (the value is an
        immutable datetime), safe to call from the agent turn."""
        deadline = self._deadlines.get(session_code)
        if deadline is None:
            return None
        return max(0.0, (deadline - datetime.now(UTC)).total_seconds())

    async def ensure_running(
        self,
        session_id: uuid.UUID,
        session_code: str,
        started_at: datetime,
        time_limit_seconds: int,
    ) -> asyncio.Task[None]:
        """Start the countdown task for a session if one isn't already running.

        Safe to call on every candidate connect — a live task is reused. Returns
        the (existing or freshly created) task so callers/tests can await it.
        """
        async with self._lock:
            # Record the deadline regardless of task reuse (idempotent — same value).
            self._deadlines[session_code] = started_at + timedelta(
                seconds=time_limit_seconds
            )
            existing = self._tasks.get(session_code)
            if existing is not None and not existing.done():
                return existing
            task = asyncio.create_task(
                self._run(session_id, session_code, started_at, time_limit_seconds)
            )
            self._tasks[session_code] = task
            return task

    def cancel(self, session_code: str) -> None:
        """Cancel the countdown task for a session (idempotent)."""
        self._deadlines.pop(session_code, None)
        task = self._tasks.pop(session_code, None)
        if task is not None and not task.done():
            task.cancel()

    async def _run(
        self,
        session_id: uuid.UUID,
        session_code: str,
        started_at: datetime,
        time_limit_seconds: int,
    ) -> None:
        deadline = started_at + timedelta(seconds=time_limit_seconds)
        try:
            while True:
                remaining = (deadline - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    break
                # Advisory frame: drop-if-full, never evict a canonical event
                # buffered for a slow consumer. The next tick supersedes.
                await broker.publish_advisory(
                    session_code,
                    {"kind": "timer_tick", "remaining_seconds": int(remaining)},
                )
                await asyncio.sleep(min(TICK_INTERVAL_SECONDS, remaining))
            await end_async_session(session_id, session_code, "timeout")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "async_timer: countdown task crashed for code=%s", session_code
            )
        finally:
            # Drop self only if still the registered task (avoid clobbering a
            # task a concurrent ensure_running may have just installed).
            if self._tasks.get(session_code) is asyncio.current_task():
                self._tasks.pop(session_code, None)
                self._deadlines.pop(session_code, None)


# Global singleton — mirror of agent_host_registry / broker patterns.
async_timer_registry = AsyncTimerRegistry()


async def recover_async_timers() -> int:
    """Re-arm countdown timers for active async sessions at backend startup.

    The countdown task is in-process, so a restart loses it. Without recovery an
    async session whose deadline lapses while the candidate is *not* connected
    would stay ``active`` forever (the timer is otherwise only armed on a
    candidate connect). On startup we re-arm every active async session already
    anchored (``started_at`` set on a prior connect): one past its deadline is
    driven to ``timeout`` immediately; one still within its window keeps ticking
    until the deadline or the next reconnect (which reuses the live task via
    :meth:`AsyncTimerRegistry.ensure_running`). Sessions never connected
    (``started_at IS NULL``) are skipped — their timer arms on first connect as
    normal. Independent of the sandbox provider (async timers are not Docker-
    specific), so it runs on every startup. Returns the count re-armed.
    """
    sm = get_sessionmaker()
    async with sm() as db:
        rows = (
            await db.execute(
                select(
                    DbSession.id,
                    DbSession.code,
                    DbSession.started_at,
                    DbSession.time_limit_seconds,
                )
                .where(DbSession.status == "active")
                .where(DbSession.mode == "async")
                .where(DbSession.started_at.is_not(None))
                .where(DbSession.time_limit_seconds.is_not(None))
            )
        ).all()

    for sid, code, started_at, time_limit_seconds in rows:
        await async_timer_registry.ensure_running(
            sid, code, started_at, time_limit_seconds
        )
    if rows:
        logger.info(
            "recover_async_timers: re-armed %d active async session(s)", len(rows)
        )
    return len(rows)


__all__ = [
    "AsyncTimerRegistry",
    "async_timer_registry",
    "end_async_session",
    "recover_async_timers",
    "TICK_INTERVAL_SECONDS",
]
