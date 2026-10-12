"""Task model CRUD + constraints.

Verifies: migration 0009 applied, tasks table writes (JSONB starter_files /
weakness / spec round-trip), unique(company_id, slug), company CASCADE deletes
its tasks, and Session.task_ref_id is SET NULL when the referenced task is
deleted (graceful degradation).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vibony_backend.db.models import Company, Session as DbSession, Task

_session_loop = pytest.mark.asyncio(loop_scope="session")


async def _make_company(db: AsyncSession, name: str = "Acme") -> Company:
    c = Company(name=name)
    db.add(c)
    await db.flush()
    return c


def _make_task(company_id: uuid.UUID, slug: str = "training-loop") -> Task:
    return Task(
        company_id=company_id,
        slug=slug,
        title="Training loop",
        description_md="# Build a training loop",
        starter_files=[{"path": "train.py", "content": "raise NotImplementedError\n"}],
        runtime_image="vibony/sandbox-py:3.12",
        weakness={"blindspots": [{"issue": "no optimizer state"}], "style": {"confidence": "high"}},
        spec={"context": "ml-infra", "contract": ["Adam"], "expected_ai_pitfalls": []},
        visibility="workspace",
    )


@_session_loop
async def test_task_insert_and_fetch(db_session: AsyncSession):
    company = await _make_company(db_session)
    t = _make_task(company.id)
    db_session.add(t)
    await db_session.flush()

    fetched = (
        await db_session.execute(select(Task).where(Task.id == t.id))
    ).scalar_one()
    assert fetched.slug == "training-loop"
    assert fetched.is_template is False
    assert fetched.visibility == "workspace"
    assert fetched.starter_files[0]["path"] == "train.py"
    assert fetched.weakness["style"]["confidence"] == "high"
    assert fetched.spec["contract"] == ["Adam"]
    assert fetched.created_at is not None


@_session_loop
async def test_task_unique_company_slug(db_session: AsyncSession):
    company = await _make_company(db_session)
    db_session.add(_make_task(company.id, "dup"))
    await db_session.flush()
    db_session.add(_make_task(company.id, "dup"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@_session_loop
async def test_same_slug_distinct_companies_ok(db_session: AsyncSession):
    a = await _make_company(db_session, "A")
    b = await _make_company(db_session, "B")
    db_session.add(_make_task(a.id, "shared-slug"))
    db_session.add(_make_task(b.id, "shared-slug"))
    await db_session.flush()  # no IntegrityError — uniqueness is per company


@_session_loop
async def test_company_cascade_deletes_tasks(db_session: AsyncSession):
    company = await _make_company(db_session)
    t = _make_task(company.id)
    db_session.add(t)
    await db_session.flush()
    task_id = t.id

    await db_session.delete(company)
    await db_session.flush()

    remaining = (
        await db_session.execute(select(Task).where(Task.id == task_id))
    ).scalars().all()
    assert remaining == []


@_session_loop
async def test_session_task_ref_set_null_on_task_delete(db_session: AsyncSession):
    company = await _make_company(db_session)
    t = _make_task(company.id)
    db_session.add(t)
    await db_session.flush()

    now = datetime.now(UTC)
    s = DbSession(
        code="TREF-0001",
        status="pending",
        task_id="training-loop",
        task_ref_id=t.id,
        protocol_version=7,
        ai_config={"primary_model": "deepseek/deepseek-chat", "fallback_models": []},
        candidate_token=str(uuid.uuid4()),
        company_id=company.id,
        expires_at=now + timedelta(hours=8),
    )
    db_session.add(s)
    await db_session.flush()

    await db_session.delete(t)
    await db_session.flush()
    await db_session.refresh(s)

    # Task gone, but the session survives with a NULL ref (slug snapshot stays).
    assert s.task_ref_id is None
    assert s.task_id == "training-loop"
