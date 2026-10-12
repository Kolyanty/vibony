"""Template seed: filesystem importer + idempotent upsert + backfill.

`import_task_from_dir` reads a repo task dir into Task column values (pure);
`seed_template_tasks` upserts the reference tasks as templates owned by the system
workspace and backfills `Session.task_ref_id` by slug.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vibony_backend.db.models import Company, Session as DbSession, Task
from vibony_backend.tasks.seed import (
    SYSTEM_COMPANY_ID,
    import_task_from_dir,
    seed_template_tasks,
)
from vibony_backend.tasks.images import DEFAULT_TEMPLATE_IMAGE

_session_loop = pytest.mark.asyncio(loop_scope="session")

_REPO_TASKS = Path(__file__).resolve().parents[2] / "tasks"


# --- import_task_from_dir (pure filesystem) ---


def test_import_training_loop_full():
    data = import_task_from_dir(_REPO_TASKS / "task-01-training-loop")
    assert data is not None
    assert data["slug"] == "task-01-training-loop"
    assert data["runtime_image"] == "vibony/sandbox-py:3.12"

    paths = {f["path"] for f in data["starter_files"]}
    assert {"train.py", "model.py", "data.py", "pyproject.toml"} <= paths
    # task.md → description, private + solution files excluded.
    assert "task.md" not in paths
    assert "spec.yaml" not in paths
    assert "weakness.yaml" not in paths
    assert "_solution.py" not in paths
    assert "INTERVIEWER.md" not in paths

    assert data["description_md"].strip()
    assert data["weakness"]["blindspots"]
    # spec JSONB carries assessment fields but not the column-promoted keys.
    assert data["spec"]["expected_ai_pitfalls"]
    assert "runtime_image" not in data["spec"]
    assert "id" not in data["spec"]


def test_import_no_spec_uses_default_image(tmp_path: Path):
    task_dir = tmp_path / "minimal-task"
    task_dir.mkdir()
    (task_dir / "weakness.yaml").write_text("weakness:\n  blindspots: []\n")
    data = import_task_from_dir(task_dir)
    assert data is not None
    assert data["slug"] == "minimal-task"
    assert data["runtime_image"] == DEFAULT_TEMPLATE_IMAGE
    assert data["spec"] == {}
    assert data["weakness"] == {"blindspots": []}


def test_import_non_task_returns_none(tmp_path: Path):
    d = tmp_path / "not-a-task"
    d.mkdir()
    (d / "readme.txt").write_text("hi")
    assert import_task_from_dir(d) is None  # no weakness.yaml


# --- seed_template_tasks (DB upsert + backfill) ---


async def _count_templates(db: AsyncSession) -> int:
    rows = (
        await db.execute(
            select(Task).where(Task.company_id == SYSTEM_COMPANY_ID)
        )
    ).scalars().all()
    return len(rows)


@_session_loop
async def test_seed_creates_system_company_and_templates(db_session: AsyncSession):
    n = await seed_template_tasks(db_session)
    assert n == 2

    sys_co = (
        await db_session.execute(
            select(Company).where(Company.id == SYSTEM_COMPANY_ID)
        )
    ).scalar_one()
    assert sys_co.name == "Vibony Templates"

    templates = (
        await db_session.execute(
            select(Task).where(Task.company_id == SYSTEM_COMPANY_ID)
        )
    ).scalars().all()
    assert {t.slug for t in templates} == {
        "task-01-training-loop",
        "task-02-seat-reservation",
    }
    assert all(t.is_template and t.visibility == "public" for t in templates)


@_session_loop
async def test_seed_idempotent(db_session: AsyncSession):
    await seed_template_tasks(db_session)
    await seed_template_tasks(db_session)  # second run upserts, no duplicates
    assert await _count_templates(db_session) == 2


@_session_loop
async def test_seed_backfills_session_task_ref(db_session: AsyncSession):
    # A legacy session referencing a template slug with no task_ref_id yet.
    c = Company(name="Employer")
    db_session.add(c)
    await db_session.flush()
    now = datetime.now(UTC)
    s = DbSession(
        code="BACKFILL1",
        status="completed",
        task_id="task-01-training-loop",
        task_ref_id=None,
        protocol_version=7,
        ai_config={"primary_model": "m", "fallback_models": []},
        candidate_token=str(uuid.uuid4()),
        company_id=c.id,
        expires_at=now + timedelta(hours=8),
    )
    db_session.add(s)
    await db_session.flush()

    await seed_template_tasks(db_session)
    await db_session.refresh(s)

    template = (
        await db_session.execute(
            select(Task).where(
                Task.company_id == SYSTEM_COMPANY_ID,
                Task.slug == "task-01-training-loop",
            )
        )
    ).scalar_one()
    assert s.task_ref_id == template.id


@_session_loop
async def test_seed_backfill_prefers_workspace_task_over_template(db_session: AsyncSession):
    """When a task-less session's slug exists BOTH as the session's own
    workspace task and as a same-slug system template, the backfill links the session
    to its own workspace task (mirroring find_task_by_slug precedence), not the template."""
    c = Company(name="Employer")
    db_session.add(c)
    await db_session.flush()

    # The workspace owns a task with the SAME slug as a reference template.
    own = Task(
        company_id=c.id,
        created_by=None,
        slug="task-01-training-loop",
        title="Own copy",
        description_md="",
        starter_files=[],
        runtime_image=DEFAULT_TEMPLATE_IMAGE,
        weakness={},
        spec={},
        visibility="workspace",
        is_template=False,
    )
    db_session.add(own)
    await db_session.flush()

    now = datetime.now(UTC)
    s = DbSession(
        code="BACKFILL2",
        status="completed",
        task_id="task-01-training-loop",
        task_ref_id=None,
        protocol_version=7,
        ai_config={"primary_model": "m", "fallback_models": []},
        candidate_token=str(uuid.uuid4()),
        company_id=c.id,
        expires_at=now + timedelta(hours=8),
    )
    db_session.add(s)
    await db_session.flush()

    await seed_template_tasks(db_session)
    await db_session.refresh(s)

    template = (
        await db_session.execute(
            select(Task).where(
                Task.company_id == SYSTEM_COMPANY_ID,
                Task.slug == "task-01-training-loop",
            )
        )
    ).scalar_one()
    assert s.task_ref_id == own.id
    assert s.task_ref_id != template.id


@_session_loop
async def test_seed_retires_removed_templates_without_losing_sessions(db_session: AsyncSession):
    from vibony_backend.tasks.store import can_use_task, find_task_by_slug, resolve_for_session

    await seed_template_tasks(db_session)
    company = Company(name="Existing workspace")
    db_session.add(company)
    await db_session.flush()

    def task(company_id, slug, *, template):
        return Task(
            company_id=company_id,
            slug=slug,
            title="Preserved task",
            description_md="Original task content",
            starter_files=[{"path": "app.py", "content": "original = True\n"}],
            runtime_image=DEFAULT_TEMPLATE_IMAGE,
            weakness={},
            spec={},
            visibility="public" if template else "workspace",
            is_template=template,
        )

    retired = task(SYSTEM_COMPANY_ID, "retired-template", template=True)
    own_copy = task(company.id, "workspace-copy", template=False)
    db_session.add_all([retired, own_copy])
    await db_session.flush()
    now = datetime.now(UTC)
    session = DbSession(
        code="RETIRED1",
        status="created",
        task_id=retired.slug,
        task_ref_id=None,  # legacy session must be backfilled before retirement
        protocol_version=7,
        ai_config={"primary_model": "m", "fallback_models": []},
        candidate_token=str(uuid.uuid4()),
        company_id=company.id,
        expires_at=now + timedelta(hours=8),
    )
    db_session.add(session)
    await db_session.flush()

    assert await seed_template_tasks(db_session) == 2
    assert await seed_template_tasks(db_session) == 2  # repeated startup
    await db_session.refresh(retired)
    await db_session.refresh(own_copy)
    await db_session.refresh(session)
    assert not retired.is_template and retired.visibility == "private"
    assert session.task_ref_id == retired.id
    loaded = await resolve_for_session(db_session, session)
    assert loaded is not None and loaded.starter_files == retired.starter_files
    assert loaded.description_md == "Original task content"
    assert await find_task_by_slug(db_session, retired.slug, company.id) is None
    assert not can_use_task(retired, user_id=uuid.uuid4(), company_ids={company.id})
    assert own_copy.visibility == "workspace" and not own_copy.is_template
    assert own_copy.starter_files == retired.starter_files
    active = (await db_session.execute(select(Task).where(Task.is_template.is_(True)))).scalars().all()
    assert {row.slug for row in active} == {"task-01-training-loop", "task-02-seat-reservation"}
