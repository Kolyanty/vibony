"""Auth model tests: Company, User, OAuthIdentity, Membership, Invite.

Covers:
- Create user + company + membership (happy path + relationships)
- FK constraint: Membership with invalid user_id → IntegrityError
- Unique email constraint on User
- Unique candidate_token constraint on Session
- DB-level cascade delete: Company deleted → Sessions deleted (ON DELETE CASCADE)
- Add OAuthIdentity for existing User
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vibony_backend.db.models import (
    Company,
    Membership,
    OAuthIdentity,
    Session as DbSession,
    User,
)


def _make_user(email: str = "test@example.com") -> User:
    return User(email=email, password_hash=None)


def _make_company(name: str = "Acme") -> Company:
    return Company(name=name)


def _make_session(code: str, company_id: uuid.UUID, created_by: uuid.UUID) -> DbSession:
    now = datetime.now(UTC)
    return DbSession(
        code=code,
        status="pending",
        task_id="task-01-training-loop",
        protocol_version=2,
        ai_config={"primary_model": "test"},
        candidate_token=str(uuid.uuid4()),
        expires_at=now + timedelta(hours=8),
        company_id=company_id,
        created_by=created_by,
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_user_company_membership_create(db_session: AsyncSession):
    """Create User + Company + admin Membership; relationships resolve correctly."""
    user = _make_user("alice@example.com")
    company = _make_company("TestCo")
    db_session.add_all([user, company])
    await db_session.flush()

    membership = Membership(user_id=user.id, company_id=company.id, role="admin")
    db_session.add(membership)
    await db_session.flush()

    await db_session.refresh(user, ["memberships"])
    await db_session.refresh(company, ["members"])

    assert len(user.memberships) == 1
    assert user.memberships[0].role == "admin"
    assert user.memberships[0].company_id == company.id
    assert len(company.members) == 1
    assert company.members[0].user_id == user.id


@pytest.mark.asyncio(loop_scope="session")
async def test_membership_fk_invalid_user(db_session: AsyncSession):
    """Membership referencing a non-existent user_id → IntegrityError."""
    company = _make_company("FKCo")
    db_session.add(company)
    await db_session.flush()

    bad_membership = Membership(
        user_id=uuid.uuid4(),  # does not exist
        company_id=company.id,
        role="interviewer",
    )
    db_session.add(bad_membership)
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio(loop_scope="session")
async def test_user_unique_email(db_session: AsyncSession):
    """Inserting two Users with the same email → IntegrityError."""
    db_session.add(_make_user("dup@example.com"))
    await db_session.flush()

    db_session.add(_make_user("dup@example.com"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio(loop_scope="session")
async def test_session_unique_candidate_token(db_session: AsyncSession):
    """Two sessions with the same candidate_token → IntegrityError."""
    token = str(uuid.uuid4())
    now = datetime.now(UTC)

    db_session.add(
        DbSession(
            code="TK-0001",
            status="pending",
            task_id="task-01-training-loop",
            protocol_version=2,
            ai_config={"primary_model": "test"},
            candidate_token=token,
            expires_at=now + timedelta(hours=8),
        )
    )
    await db_session.flush()

    db_session.add(
        DbSession(
            code="TK-0002",
            status="pending",
            task_id="task-01-training-loop",
            protocol_version=2,
            ai_config={"primary_model": "test"},
            candidate_token=token,  # same token
            expires_at=now + timedelta(hours=8),
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio(loop_scope="session")
async def test_cascade_delete_company_removes_sessions(db_session: AsyncSession):
    """Deleting a Company cascades to its Sessions at the DB level."""
    user = _make_user("cascade@example.com")
    company = _make_company("CascadeCo")
    db_session.add_all([user, company])
    await db_session.flush()

    session = _make_session("CASC-0001", company.id, user.id)
    db_session.add(session)
    await db_session.flush()

    # Delete via raw SQL to exercise FK ON DELETE CASCADE (not ORM cascade).
    await db_session.execute(
        sa.delete(Company).where(Company.id == company.id)
    )
    await db_session.flush()

    remaining = (
        await db_session.execute(
            sa.select(DbSession).where(DbSession.code == "CASC-0001")
        )
    ).scalars().all()
    assert remaining == []


@pytest.mark.asyncio(loop_scope="session")
async def test_add_oauth_identity_for_user(db_session: AsyncSession):
    """Create a User then attach an OAuthIdentity; relationship resolves."""
    user = _make_user("oauth@example.com")
    db_session.add(user)
    await db_session.flush()

    identity = OAuthIdentity(
        provider="oidc",
        subject="oidc-sub-12345",
        user_id=user.id,
        email="oauth@example.com",
    )
    db_session.add(identity)
    await db_session.flush()

    await db_session.refresh(user, ["oauth_identities"])
    assert len(user.oauth_identities) == 1
    assert user.oauth_identities[0].provider == "oidc"
    assert user.oauth_identities[0].subject == "oidc-sub-12345"
