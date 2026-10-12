"""Data lifecycle: session subject-erasure + audit retention window. The
cross-border guard lives in test_chat_hardening; the audit email-resolve-at-read
lives in test_audit_log.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vibony_backend.db.models import AuditLog, Event
from vibony_backend.db.models import Session as DbSession
from vibony_backend.db.models import SessionAssessment

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _create_session(client) -> str:
    resp = await client.post(
        "/sessions",
        json={
            "task_id": "task-01-training-loop",
            "ai_config": {"primary_model": "deepseek/deepseek-chat"},
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_code"]


async def test_erase_session_hard_deletes_candidate_pii(authed_client, db_engine):
    """DELETE /sessions/{code} hard-deletes the session + its transcript +
    assessment (DB cascade) and scrubs the session's audit trail."""
    client, _company_id = authed_client
    code = await _create_session(client)

    sm = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    async with sm() as db:
        row = (
            await db.execute(select(DbSession).where(DbSession.code == code))
        ).scalar_one()
        session_id = row.id
        db.add(
            Event(
                session_id=session_id,
                seq=1,
                id=uuid.uuid4(),
                type="user_message",
                client_timestamp=datetime.now(timezone.utc),
                payload={"text": "candidate PII transcript"},
            )
        )
        db.add(
            SessionAssessment(
                session_id=session_id, status="ready", summary_md="x", findings=[]
            )
        )
        await db.commit()

    resp = await client.delete(f"/sessions/{code}")
    assert resp.status_code == 204, resp.text

    async with sm() as db:
        assert (
            await db.execute(select(DbSession).where(DbSession.id == session_id))
        ).scalar_one_or_none() is None
        # Cascades removed the transcript + assessment.
        assert (
            await db.execute(select(Event).where(Event.session_id == session_id))
        ).first() is None
        assert (
            await db.execute(
                select(SessionAssessment).where(
                    SessionAssessment.session_id == session_id
                )
            )
        ).first() is None
        # The session.created audit row (target_id == code) is scrubbed too.
        leftover = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.target_type == "session", AuditLog.target_id == code
                )
            )
        ).scalars().all()
        assert leftover == []


async def test_erase_session_requires_membership(authed_client, db_engine):
    """A caller with no membership in the session's workspace can't erase it (403)."""
    client, _company_id = authed_client
    code = await _create_session(client)

    # Switch to an outsider account (signup logs them in, no membership here).
    await client.post(
        "/auth/signup",
        json={
            "email": "eraser_outsider@e.com",
            "password": "password123",
            "company_name": "Other",
        },
    )
    resp = await client.delete(f"/sessions/{code}")
    assert resp.status_code == 403

    # The session survived the rejected erase.
    sm = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    async with sm() as db:
        assert (
            await db.execute(select(DbSession).where(DbSession.code == code))
        ).scalar_one_or_none() is not None


async def test_erase_session_unauthenticated_401(authed_client):
    client, _company_id = authed_client
    code = await _create_session(client)
    await client.post("/auth/logout")
    resp = await client.delete(f"/sessions/{code}")
    assert resp.status_code == 401


async def test_audit_retention_prunes_old_rows(authed_client, db_engine):
    """prune_older_than removes rows past the window, keeps recent ones."""
    client, company_id = authed_client
    sm = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)

    old_id, fresh_id = uuid.uuid4(), uuid.uuid4()
    async with sm() as db:
        db.add(
            AuditLog(
                id=old_id,
                company_id=uuid.UUID(company_id),
                actor_user_id=None,
                action="member.invited",
                created_at=datetime.now(timezone.utc) - timedelta(days=400),
            )
        )
        db.add(
            AuditLog(
                id=fresh_id,
                company_id=uuid.UUID(company_id),
                actor_user_id=None,
                action="member.invited",
                created_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()

    from vibony_backend.auth import audit

    pruned = await audit.prune_older_than(365)
    assert pruned >= 1

    async with sm() as db:
        assert (
            await db.execute(select(AuditLog).where(AuditLog.id == old_id))
        ).scalar_one_or_none() is None
        assert (
            await db.execute(select(AuditLog).where(AuditLog.id == fresh_id))
        ).scalar_one_or_none() is not None


async def test_audit_retention_zero_is_noop(authed_client, db_engine):
    from vibony_backend.auth import audit

    assert await audit.prune_older_than(0) == 0
    assert await audit.prune_older_than(-5) == 0
