"""Task store (resolution + access) + render_weakness_dict.

`load_task` / `resolve_for_session` are the runtime DB seam; `can_use_task`
is the session-create access predicate; `render_weakness_dict` renders the system
prompt from a stored weakness mapping.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from vibony_backend.db.models import Company, Session as DbSession, Task
from vibony_backend.tasks.store import (
    LoadedTask,
    can_use_task,
    load_task,
    resolve_for_session,
)
from vibony_backend.weakness import render_weakness_dict

_session_loop = pytest.mark.asyncio(loop_scope="session")


async def _company(db: AsyncSession, name: str) -> Company:
    c = Company(name=name)
    db.add(c)
    await db.flush()
    return c


async def _task(
    db: AsyncSession,
    company_id: uuid.UUID,
    *,
    slug: str = "t",
    visibility: str = "workspace",
    is_template: bool = False,
    created_by: uuid.UUID | None = None,
    image: str = "vibony/sandbox-py:3.12",
) -> Task:
    t = Task(
        company_id=company_id,
        created_by=created_by,
        slug=slug,
        title=slug,
        description_md="d",
        starter_files=[],
        runtime_image=image,
        weakness={},
        spec={},
        visibility=visibility,
        is_template=is_template,
    )
    db.add(t)
    await db.flush()
    return t


def _session(company_id, task_id, task_ref_id) -> DbSession:
    now = datetime.now(UTC)
    return DbSession(
        code=f"S-{uuid.uuid4().hex[:8]}",
        status="pending",
        task_id=task_id,
        task_ref_id=task_ref_id,
        protocol_version=7,
        ai_config={"primary_model": "m", "fallback_models": []},
        candidate_token=str(uuid.uuid4()),
        company_id=company_id,
        expires_at=now + timedelta(hours=8),
    )


@_session_loop
async def test_load_task_returns_loaded(db_session: AsyncSession):
    c = await _company(db_session, "A")
    t = await _task(db_session, c.id, slug="abc")
    loaded = await load_task(db_session, t.id)
    assert isinstance(loaded, LoadedTask)
    assert loaded.slug == "abc"
    assert loaded.runtime_image == "vibony/sandbox-py:3.12"


@_session_loop
async def test_load_task_missing_returns_none(db_session: AsyncSession):
    assert await load_task(db_session, uuid.uuid4()) is None


@_session_loop
async def test_resolve_for_session_by_ref(db_session: AsyncSession):
    c = await _company(db_session, "A")
    t = await _task(db_session, c.id, slug="by-ref")
    s = _session(c.id, "by-ref", t.id)
    db_session.add(s)
    await db_session.flush()
    loaded = await resolve_for_session(db_session, s)
    assert loaded is not None and loaded.id == t.id


@_session_loop
async def test_resolve_for_session_slug_fallback(db_session: AsyncSession):
    c = await _company(db_session, "A")
    t = await _task(db_session, c.id, slug="slugfb")
    s = _session(c.id, "slugfb", None)  # no ref → slug fallback
    db_session.add(s)
    await db_session.flush()
    loaded = await resolve_for_session(db_session, s)
    assert loaded is not None and loaded.id == t.id


@_session_loop
async def test_resolve_prefers_company_over_template(db_session: AsyncSession):
    sys = await _company(db_session, "System")
    own = await _company(db_session, "Own")
    template = await _task(db_session, sys.id, slug="shared", is_template=True)
    mine = await _task(db_session, own.id, slug="shared", is_template=False)
    s = _session(own.id, "shared", None)
    db_session.add(s)
    await db_session.flush()
    loaded = await resolve_for_session(db_session, s)
    assert loaded is not None and loaded.id == mine.id  # company task wins
    assert template.id != mine.id


# Note: a *dangling* task_ref_id can't occur — the FK with ON DELETE SET NULL
# turns a deleted task's ref into NULL (the slug-fallback case above), and the FK
# rejects inserting a non-existent ref. resolve_for_session keeps a defensive
# None-check on load_task only for a cross-transaction delete race.


# --- can_use_task access matrix (pure function — in-memory Task, no DB) ---


def _mem_task(
    *,
    company_id: uuid.UUID,
    visibility: str = "workspace",
    is_template: bool = False,
    created_by: uuid.UUID | None = None,
) -> Task:
    return Task(
        company_id=company_id,
        created_by=created_by,
        slug="t",
        title="t",
        description_md="",
        starter_files=[],
        runtime_image="vibony/sandbox-py:3.12",
        weakness={},
        spec={},
        visibility=visibility,
        is_template=is_template,
    )


def test_can_use_template_by_anyone():
    t = _mem_task(company_id=uuid.uuid4(), is_template=True, visibility="public")
    assert can_use_task(t, user_id=uuid.uuid4(), company_ids=set())


def test_can_use_workspace_requires_membership():
    cid = uuid.uuid4()
    t = _mem_task(company_id=cid, visibility="workspace")
    assert can_use_task(t, user_id=uuid.uuid4(), company_ids={cid})
    assert not can_use_task(t, user_id=uuid.uuid4(), company_ids={uuid.uuid4()})


def test_can_use_private_requires_author():
    cid = uuid.uuid4()
    author = uuid.uuid4()
    t = _mem_task(company_id=cid, visibility="private", created_by=author)
    assert can_use_task(t, user_id=author, company_ids={cid})
    assert not can_use_task(t, user_id=uuid.uuid4(), company_ids={cid})


# --- render_weakness_dict ---


def test_render_weakness_dict_includes_blindspot():
    weakness = {
        "blindspots": [{"issue": "checkpoint saves only model state"}],
        "style": {"confidence": "high"},
    }
    prompt = render_weakness_dict(weakness)
    assert "checkpoint saves only model state" in prompt
    assert prompt.strip()  # global preamble present


def test_render_weakness_dict_empty_is_global_only():
    prompt = render_weakness_dict({})
    assert prompt.strip()  # still renders the global preamble, no crash
    assert "## Blind spots" not in prompt  # no blindspots section
