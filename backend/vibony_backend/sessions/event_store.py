"""Shared event storage: write to DB + publish to broker.

Used by AgentHost's BackendEventSink — the only event producer since the legacy
CLI WS ingestion path was removed. ``store_event`` is the "fire-and-maybe-raise"
writer for server-generated events: it row-locks the session, dedups by
event_id, checks invariants, assigns seq, writes the row and publishes to the
broker, applying the session_start→active / session_end→completed transitions.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update

from vibony_backend.db.engine import get_sessionmaker
from vibony_backend.db.models import Event
from vibony_backend.db.models import Session as DbSession
from vibony_backend.sessions.broker import broker
from vibony_backend.sessions.invariants import check_invariants

logger = logging.getLogger(__name__)


async def replay_messages_for_agent(session_code: str) -> list[dict[str, Any]]:
    """Reconstruct LLM message history from stored user_message/assistant_message events.

    Returns a list of {role, content} dicts suitable for passing to the LLM.
    The welcome assistant_message (before any user_message) is excluded to keep
    the history starting with the user role, which most LLM APIs require.
    Multiple assistant_message events within one turn are joined with newlines.
    Returns [] if the session has no conversation history or doesn't exist.
    """
    sm = get_sessionmaker()
    async with sm() as db:
        rows = (
            await db.execute(
                select(Event.type, Event.payload)
                .join(DbSession, Event.session_id == DbSession.id)
                .where(DbSession.code == session_code)
                .where(Event.type.in_(["user_message", "assistant_message"]))
                .order_by(Event.seq)
            )
        ).all()

    messages: list[dict[str, Any]] = []
    seen_user = False
    pending_assistant: list[str] = []

    def _flush_assistant() -> None:
        if pending_assistant:
            combined = "\n".join(t for t in pending_assistant if t)
            if combined:
                messages.append({"role": "assistant", "content": combined})
        pending_assistant.clear()

    for event_type, payload in rows:
        if event_type == "user_message":
            if seen_user:
                _flush_assistant()
            seen_user = True
            messages.append({"role": "user", "content": payload.get("text", "")})
        elif event_type == "assistant_message" and seen_user:
            text = payload.get("text", "")
            if text:
                pending_assistant.append(text)

    _flush_assistant()
    return messages


async def load_agent_messages(session_code: str) -> list[dict[str, Any]] | None:
    """Load the persisted live LLM message blob for a session.

    Returns the verbatim ``AgentHost._messages`` list saved after the session's
    last completed turn — lossless, including tool-call rounds. Returns ``None``
    when the session has no blob: a legacy session, a fresh session before its
    first turn, or a missing session. The caller (AgentHost._restore_messages)
    falls back to the lossy user/assistant event replay in that case.
    """
    sm = get_sessionmaker()
    async with sm() as db:
        result = await db.execute(
            select(DbSession.agent_messages).where(DbSession.code == session_code)
        )
        return result.scalar_one_or_none()


async def save_agent_messages(
    session_code: str, messages: list[dict[str, Any]]
) -> None:
    """Persist the live LLM message blob for a session.

    Overwrites ``sessions.agent_messages`` with the current ``_messages`` after a
    completed turn. Single-writer per session (serialised by
    ``AgentHost._turn_lock``), so no concurrent-write race. This artifact is
    canonical for LLM continuation; the event log stays canonical for the
    dashboard/replay.
    """
    sm = get_sessionmaker()
    async with sm() as db:
        async with db.begin():
            result = await db.execute(
                update(DbSession)
                .where(DbSession.code == session_code)
                .values(agent_messages=messages)
            )
    if result.rowcount == 0:
        from vibony_backend.sessions.runtime_store import PersistenceError
        raise PersistenceError("Session no longer exists; conversation was not saved")


async def mark_async_started(session_id: uuid.UUID) -> datetime | None:
    """Anchor the async deadline: set ``sessions.started_at`` to now on first
    call, idempotent thereafter.

    Returns the effective ``started_at`` (the just-set value on first connect,
    or the existing one on reconnect) so the caller can compute the server-side
    deadline (``started_at + time_limit_seconds``). Returns ``None`` if the
    session row is missing. Uses ``FOR UPDATE`` so concurrent first connects
    cannot both write a fresh anchor.
    """
    sm = get_sessionmaker()
    async with sm() as db:
        async with db.begin():
            locked = (
                await db.execute(
                    select(DbSession)
                    .where(DbSession.id == session_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if locked is None:
                return None
            if locked.started_at is None:
                locked.started_at = datetime.now(UTC)
            return locked.started_at


async def replay_file_state(
    session_code: str,
) -> dict[str, str | None]:
    """Build a per-path projection of the latest known file content from the event log.

    Returns ``{path: content}`` for files that should exist with the given UTF-8
    string content, and ``{path: None}`` for paths whose last recorded event was a
    deletion. Directory events (``is_dir: true``), baseline events (``source:
    baseline`` — the scaffold, already restored by put_archive) and events
    without ``content`` (binary files from old CLI sessions) are skipped —
    restoration only applies text-file changes the sandbox can replay losslessly.

    Used by AgentHost during web-mode provisioning to restore the sandbox FS
    after a backend restart. The returned projection is O(unique_files), but the
    scan loads every file_changed payload (full content included) into memory —
    O(total events). Acceptable at current per-session scale; a future
    optimisation could push the last-write-wins fold into SQL via
    ``DISTINCT ON (file_path) ... ORDER BY file_path, seq DESC``.
    """
    sm = get_sessionmaker()
    async with sm() as db:
        rows = (
            await db.execute(
                select(Event.payload)
                .join(DbSession, Event.session_id == DbSession.id)
                .where(DbSession.code == session_code)
                .where(Event.type == "file_changed")
                .order_by(Event.seq)
            )
        ).all()

    projection: dict[str, str | None] = {}
    for (payload,) in rows:
        if not isinstance(payload, dict):
            continue
        if payload.get("is_dir"):
            continue
        # Baseline events are the task scaffold, already laid down by
        # put_archive on the fresh sandbox. Skipping them keeps the projection
        # empty for an untouched session, so cold-start restore (and its banner)
        # fires only for genuine candidate/AI edits.
        if payload.get("source") == "baseline":
            continue
        path = payload.get("file_path")
        if not isinstance(path, str) or not path:
            continue
        op = payload.get("op", "modified")
        if op == "deleted":
            projection[path] = None
            continue
        # Missing "content" key = binary/old-CLI event we cannot reconstruct
        # (skip). An explicit content=None is a tombstone (treat as deletion).
        if "content" not in payload:
            continue
        content = payload.get("content")
        if content is None:
            projection[path] = None
            continue
        if not isinstance(content, str):
            continue
        projection[path] = content
    return projection


async def has_assistant_message(session_code: str) -> bool:
    """Return True if any assistant_message event exists for the session.

    Used by AgentHost to skip re-emitting the welcome on cold-start.
    """
    sm = get_sessionmaker()
    async with sm() as db:
        result = await db.execute(
            select(Event.seq)
            .join(DbSession, Event.session_id == DbSession.id)
            .where(DbSession.code == session_code)
            .where(Event.type == "assistant_message")
            .limit(1)
        )
        return result.scalar_one_or_none() is not None


async def store_event(
    session_id: uuid.UUID,
    session_code: str,
    event_type: str,
    payload: dict[str, Any],
    event_id: uuid.UUID | None = None,
    client_ts: datetime | None = None,
) -> int:
    """Write a validated event to DB and publish to broker. Returns seq.

    Raises RuntimeError if session is missing or completed.
    Returns 0 and no-ops silently on dedup (same event_id already stored).
    Invariant violations raise RuntimeError.
    """
    if event_id is None:
        event_id = uuid.uuid4()
    if client_ts is None:
        client_ts = datetime.now(UTC)

    sm = get_sessionmaker()
    next_seq = 0

    async with sm() as db:
        async with db.begin():
            locked = (
                await db.execute(
                    select(DbSession)
                    .where(DbSession.id == session_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()

            if locked is None:
                raise RuntimeError(f"store_event: session {session_id} not found")

            if locked.status == "completed":
                raise RuntimeError(
                    f"store_event: session {session_code} is already completed"
                )

            # Dedup: same event_id already stored → no-op.
            existing_seq = (
                await db.execute(
                    select(Event.seq).where(
                        Event.session_id == session_id,
                        Event.id == event_id,
                    )
                )
            ).scalar_one_or_none()

            if existing_seq is not None:
                logger.debug(
                    "store_event: dedup code=%s type=%s id=%s",
                    session_code,
                    event_type,
                    event_id,
                )
                return 0

            inv_err = await check_invariants(db, session_id, event_type, payload)
            if inv_err:
                raise RuntimeError(
                    f"store_event: invariant violation in {event_type}: {inv_err}"
                )

            max_seq: int = (
                await db.execute(
                    select(func.coalesce(func.max(Event.seq), 0)).where(
                        Event.session_id == session_id
                    )
                )
            ).scalar() or 0
            next_seq = max_seq + 1

            db.add(
                Event(
                    session_id=session_id,
                    seq=next_seq,
                    id=event_id,
                    type=event_type,
                    client_timestamp=client_ts,
                    payload=payload,
                )
            )

            if event_type == "session_start" and locked.status == "pending":
                locked.status = "active"
            elif event_type == "session_end" and locked.status == "active":
                locked.status = "completed"
                locked.completed_at = datetime.now(UTC)
                locked.completed_reason = payload.get("reason", "candidate_quit")

            await db.flush()

    envelope: dict[str, Any] = {
        "kind": "event",
        "event": {
            "id": str(event_id),
            "seq": next_seq,
            "session_code": session_code,
            "timestamp": client_ts.isoformat(),
            "type": event_type,
            "payload": payload,
        },
    }
    await broker.publish(session_code, envelope)
    logger.debug(
        "store_event: code=%s seq=%d type=%s", session_code, next_seq, event_type
    )
    return next_seq
