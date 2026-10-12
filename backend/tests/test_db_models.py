"""Basic CRUD smoke on the Session/Event models.

Checks: migrations applied, both tables are written, FKs work,
the `(session_id, id)` unique constraint catches a duplicate event.id, JSONB
stores nested structures, ondelete CASCADE removes events.

owner_token removed; candidate_token (uuid4) used instead.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vibony_backend.db.models import Event, Session as DbSession


def _make_session(code: str = "ABCD-1234") -> DbSession:
    now = datetime.now(UTC)
    return DbSession(
        code=code,
        status="pending",
        task_id="task-01-training-loop",
        protocol_version=2,
        ai_config={
            "primary_model": "deepseek/deepseek-chat",
            "fallback_models": ["anthropic/claude-haiku-4-5"],
        },
        candidate_token=str(uuid.uuid4()),
        expires_at=now + timedelta(hours=8),
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_session_insert_and_fetch(db_session: AsyncSession):
    s = _make_session()
    db_session.add(s)
    await db_session.flush()

    fetched = (
        await db_session.execute(select(DbSession).where(DbSession.code == "ABCD-1234"))
    ).scalar_one()

    assert fetched.id == s.id
    assert fetched.status == "pending"
    assert fetched.ai_config["primary_model"] == "deepseek/deepseek-chat"
    assert fetched.ai_config["fallback_models"] == ["anthropic/claude-haiku-4-5"]
    assert fetched.created_at is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_session_code_unique(db_session: AsyncSession):
    db_session.add(_make_session("DUPE-0001"))
    await db_session.flush()
    db_session.add(_make_session("DUPE-0001"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio(loop_scope="session")
async def test_event_insert_and_link(db_session: AsyncSession):
    s = _make_session("LINK-0001")
    db_session.add(s)
    await db_session.flush()

    ev_id = uuid.uuid4()
    db_session.add(
        Event(
            session_id=s.id,
            seq=1,
            id=ev_id,
            type="session_start",
            client_timestamp=datetime.now(UTC),
            payload={"task_id": "task-01-training-loop", "protocol_version": 1},
        )
    )
    await db_session.flush()

    events = (
        await db_session.execute(
            select(Event).where(Event.session_id == s.id).order_by(Event.seq)
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].id == ev_id
    assert events[0].payload["task_id"] == "task-01-training-loop"


@pytest.mark.asyncio(loop_scope="session")
async def test_event_id_unique_within_session(db_session: AsyncSession):
    s = _make_session("DUP-EV-01")
    db_session.add(s)
    await db_session.flush()

    ev_id = uuid.uuid4()
    db_session.add(
        Event(
            session_id=s.id,
            seq=1,
            id=ev_id,
            type="user_message",
            client_timestamp=datetime.now(UTC),
            payload={"text": "hi"},
        )
    )
    await db_session.flush()

    db_session.add(
        Event(
            session_id=s.id,
            seq=2,
            id=ev_id,  # same id → must fail
            type="user_message",
            client_timestamp=datetime.now(UTC),
            payload={"text": "hi again"},
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio(loop_scope="session")
async def test_event_seq_pk(db_session: AsyncSession):
    """`(session_id, seq)` is the PK; a repeated pair is rejected."""

    s = _make_session("SEQ-PK-01")
    db_session.add(s)
    await db_session.flush()

    db_session.add(
        Event(
            session_id=s.id,
            seq=1,
            id=uuid.uuid4(),
            type="user_message",
            client_timestamp=datetime.now(UTC),
            payload={},
        )
    )
    await db_session.flush()

    db_session.add(
        Event(
            session_id=s.id,
            seq=1,  # same seq
            id=uuid.uuid4(),
            type="user_message",
            client_timestamp=datetime.now(UTC),
            payload={},
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio(loop_scope="session")
async def test_event_cascade_delete(db_session: AsyncSession):
    s = _make_session("CASC-0001")
    db_session.add(s)
    await db_session.flush()

    db_session.add(
        Event(
            session_id=s.id,
            seq=1,
            id=uuid.uuid4(),
            type="user_message",
            client_timestamp=datetime.now(UTC),
            payload={"text": "x"},
        )
    )
    await db_session.flush()

    await db_session.delete(s)
    await db_session.flush()

    remaining = (
        await db_session.execute(select(Event).where(Event.session_id == s.id))
    ).scalars().all()
    assert remaining == []
