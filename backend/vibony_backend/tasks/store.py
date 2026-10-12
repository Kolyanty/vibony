"""Task resolution + access — the runtime DB seam.

`LoadedTask` is the immutable view the runtime consumes (AgentHost provisioning +
weakness prompt, assessment spec, bundle). The pure renderers/parsers
(`weakness.render`, `assessment.inputs.parse_task_spec`) take the JSONB mappings
verbatim — this module is only about *where the dict comes from*.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vibony_backend.db.models import Session as DbSession
from vibony_backend.db.models import Task


@dataclass(frozen=True)
class LoadedTask:
    """Runtime view of a task — what AgentHost / assessment / bundle need."""

    id: uuid.UUID
    company_id: uuid.UUID
    slug: str
    title: str
    description_md: str
    starter_files: list[dict[str, Any]]
    runtime_image: str
    weakness: dict[str, Any]
    spec: dict[str, Any]
    visibility: str
    is_template: bool


def loaded_from_snapshot(snapshot: dict[str, Any]) -> LoadedTask:
    values = dict(snapshot)
    for key in ("id", "company_id"):
        values[key] = uuid.UUID(values[key])
    return LoadedTask(**values)


def loaded_from_row(row: Task) -> LoadedTask:
    return LoadedTask(
        id=row.id,
        company_id=row.company_id,
        slug=row.slug,
        title=row.title,
        description_md=row.description_md or "",
        starter_files=list(row.starter_files or []),
        runtime_image=row.runtime_image,
        weakness=dict(row.weakness or {}),
        spec=dict(row.spec or {}),
        visibility=row.visibility,
        is_template=row.is_template,
    )


async def load_task(db: AsyncSession, task_ref_id: uuid.UUID) -> LoadedTask | None:
    """Load a task by its primary key. ``None`` when it no longer exists."""
    row = await db.get(Task, task_ref_id)
    return loaded_from_row(row) if row is not None else None


async def find_task_by_slug(
    db: AsyncSession, slug: str, company_id: uuid.UUID | None
) -> Task | None:
    """Resolve a task row by slug within a workspace's scope.

    Matches a task in ``company_id`` or any system template, preferring the
    workspace's own task over a same-slug template (``is_template`` False sorts
    first). Shared by session-create resolution + the slug fallback so the
    workspace-vs-template precedence rule lives in one place.
    """
    stmt = (
        select(Task)
        .where(
            Task.slug == slug,
            or_(Task.company_id == company_id, Task.is_template.is_(True)),
        )
        .order_by(Task.is_template.asc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _resolve_by_slug(
    db: AsyncSession, slug: str, company_id: uuid.UUID | None
) -> LoadedTask | None:
    """Best-effort slug fallback for sessions with no/dropped ``task_ref_id``."""
    row = await find_task_by_slug(db, slug, company_id)
    return loaded_from_row(row) if row is not None else None


async def resolve_for_session(
    db: AsyncSession, session: DbSession
) -> LoadedTask | None:
    """Resolve the task a session runs.

    The first-start checkpoint wins. Without it, use the ``task_ref_id`` FK;
    if it's NULL (a legacy row, not yet
    backfilled) or dangling (task deleted → SET NULL), fall back to the ``task_id``
    slug snapshot. ``None`` only when nothing resolves — the runtime then degrades
    gracefully (empty bundle / near-empty assessment).
    """
    snapshot = (session.agent_state or {}).get("task")
    if snapshot:
        return loaded_from_snapshot(snapshot)
    if session.task_ref_id is not None:
        loaded = await load_task(db, session.task_ref_id)
        if loaded is not None:
            return loaded
    return await _resolve_by_slug(db, session.task_id, session.company_id)


def can_use_task(
    task: Task | LoadedTask,
    *,
    user_id: uuid.UUID | None,
    company_ids: set[uuid.UUID],
    created_by: uuid.UUID | None = None,
) -> bool:
    """Whether ``user_id`` may create a session from this task.

    - templates (and any ``public`` task) — usable by anyone authenticated;
    - ``workspace`` — the user must be a member of the owning workspace;
    - ``private`` — additionally the user must be the author.

    ``LoadedTask`` carries no ``created_by``; pass it explicitly when checking one.
    """
    if isinstance(task, Task):
        created_by = task.created_by
    if task.is_template or task.visibility == "public":
        return True
    if task.company_id not in company_ids:
        return False
    if task.visibility == "private":
        return created_by is not None and created_by == user_id
    return True  # workspace-visible and the user is a member


__all__ = [
    "LoadedTask",
    "can_use_task",
    "find_task_by_slug",
    "load_task",
    "loaded_from_row",
    "resolve_for_session",
]
