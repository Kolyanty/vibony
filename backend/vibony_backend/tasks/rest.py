"""Task CRUD REST.

Authoring surface for the dashboard. All endpoints require cookie auth
(`current_user`). Access model:

- **templates** (`is_template`) — visible read-only to everyone; never editable.
- **workspace** tasks — visible + editable by any member of the owning workspace.
- **private** tasks — visible + editable only by the author (within their workspace).
- **public** (non-template) — not surfaced cross-workspace (treated like a
  workspace task of its owner); the API does not let a user *set* public.

`GET /tasks` lists templates + the caller's visible workspace tasks (no
cross-workspace public listing). Mutations validate the runtime_image whitelist,
slug shape/uniqueness, and reject private/scaffold filenames in starter_files.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vibony_backend.auth.deps import current_user
from vibony_backend.db.engine import session_scope
from vibony_backend.db.models import Membership, Task, TaskFavorite, User
from vibony_backend.tasks.schemas import (
    TaskCreate,
    TaskListItem,
    TaskListResponse,
    TaskResponse,
    TaskUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"])


async def _company_ids(db: AsyncSession, user: User) -> set[uuid.UUID]:
    rows = (
        await db.execute(
            select(Membership.company_id).where(Membership.user_id == user.id)
        )
    ).scalars().all()
    return set(rows)


def _access(
    task: Task, *, user_id: uuid.UUID, company_ids: set[uuid.UUID]
) -> tuple[bool, bool]:
    """Return ``(visible, can_edit)`` for ``task`` from the caller's perspective."""
    if task.is_template:
        return True, False  # read-only built-in
    if task.company_id not in company_ids:
        return False, False
    ok = task.visibility != "private" or task.created_by == user_id
    return ok, ok


def _to_response(task: Task, *, can_edit: bool) -> TaskResponse:
    resp = TaskResponse.model_validate(task)
    resp.can_edit = can_edit
    return resp


async def _is_favorited(db: AsyncSession, *, user_id: uuid.UUID, task_id: uuid.UUID) -> bool:
    row = (
        await db.execute(
            select(TaskFavorite.id)
            .where(TaskFavorite.user_id == user_id, TaskFavorite.task_id == task_id)
            .limit(1)
        )
    ).first()
    return row is not None


async def _load_visible_task(
    db: AsyncSession, task_id: uuid.UUID, user: User
) -> tuple[Task, bool]:
    """Load a task the caller may see, else 404 (don't leak existence). Returns
    ``(task, can_edit)``."""
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail={"reason": "not_found"})
    company_ids = await _company_ids(db, user)
    visible, can_edit = _access(task, user_id=user.id, company_ids=company_ids)
    if not visible:
        raise HTTPException(status_code=404, detail={"reason": "not_found"})
    return task, can_edit


# Cap the whitespace-split token count so a pathological query can't fan out into a
# huge AND chain (the 200-char q cap already bounds total length).
MAX_SEARCH_TOKENS = 12


def _escape_like(term: str) -> str:
    """Neutralize ILIKE wildcards so a user's ``%``/``_`` match literally (used with
    ``escape='\\'``). Backslash first, so we don't double-escape our own escapes."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    db: AsyncSession = Depends(session_scope),
    user: User = Depends(current_user),
    q: str | None = Query(default=None, max_length=200),
) -> TaskListResponse:
    """Templates + the caller's visible workspace tasks (no cross-workspace public).

    Optional ``q`` filters by a case-insensitive substring of the title, slug, or
    description — the catalog search. Wildcards in ``q`` are escaped so they match
    literally, and the term is length-capped by the query validator.
    """
    company_ids = await _company_ids(db, user)
    stmt = select(Task).where(
        or_(
            Task.is_template.is_(True),
            and_(
                Task.company_id.in_(company_ids),
                or_(Task.visibility != "private", Task.created_by == user.id),
            ),
        )
    )
    # Tokenize on whitespace and AND the tokens — each may match a different field, so
    # "ab test" finds "A/B Test" (via title) + "…-ab-test" (via slug) even though the
    # whole phrase is a substring of neither.
    for token in (q.split() if q else [])[:MAX_SEARCH_TOKENS]:
        like = f"%{_escape_like(token)}%"
        stmt = stmt.where(
            or_(
                Task.title.ilike(like, escape="\\"),
                Task.slug.ilike(like, escape="\\"),
                Task.description_md.ilike(like, escape="\\"),
            )
        )
    stmt = stmt.order_by(Task.is_template.asc(), Task.updated_at.desc())
    rows = (await db.execute(stmt)).scalars().all()

    # The caller's personal favorites (one small query, mapped onto the list).
    favorite_ids = set(
        (
            await db.execute(
                select(TaskFavorite.task_id).where(TaskFavorite.user_id == user.id)
            )
        ).scalars().all()
    )

    items: list[TaskListItem] = []
    for row in rows:
        _, can_edit = _access(row, user_id=user.id, company_ids=company_ids)
        item = TaskListItem.model_validate(row)
        item.can_edit = can_edit
        item.is_favorite = row.id in favorite_ids
        items.append(item)
    return TaskListResponse(tasks=items)


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(session_scope),
    user: User = Depends(current_user),
) -> TaskResponse:
    task, can_edit = await _load_visible_task(db, task_id, user)
    resp = _to_response(task, can_edit=can_edit)
    resp.is_favorite = await _is_favorited(db, user_id=user.id, task_id=task.id)
    return resp


@router.put("/{task_id}/favorite", status_code=status.HTTP_204_NO_CONTENT)
async def add_favorite(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(session_scope),
    user: User = Depends(current_user),
) -> None:
    """Mark ``task_id`` as the caller's favorite (idempotent). 404 if unseeable."""
    # Gate on visibility so a favorite can't probe the existence of a task the
    # caller may not see; reuses the same 404-not-leak rule as reads.
    await _load_visible_task(db, task_id, user)
    await db.execute(
        pg_insert(TaskFavorite)
        .values(user_id=user.id, task_id=task_id)
        .on_conflict_do_nothing(index_elements=["user_id", "task_id"])
    )
    await db.commit()


@router.delete("/{task_id}/favorite", status_code=status.HTTP_204_NO_CONTENT)
async def remove_favorite(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(session_scope),
    user: User = Depends(current_user),
) -> None:
    """Drop the caller's favorite for ``task_id`` (idempotent, no visibility gate).

    A favorite is the caller's own data — removing it is always allowed, even for a
    task they can no longer see, so a stale favorite is never un-removable.
    """
    await db.execute(
        delete(TaskFavorite).where(
            TaskFavorite.user_id == user.id, TaskFavorite.task_id == task_id
        )
    )
    await db.commit()


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreate,
    db: AsyncSession = Depends(session_scope),
    user: User = Depends(current_user),
) -> TaskResponse:
    company_ids = await _company_ids(db, user)
    if not company_ids:
        raise HTTPException(status_code=403, detail={"reason": "no_membership"})

    if payload.workspace_id is not None:
        if payload.workspace_id not in company_ids:
            raise HTTPException(status_code=403, detail={"reason": "unauthorized"})
        company_id = payload.workspace_id
    elif len(company_ids) == 1:
        company_id = next(iter(company_ids))
    else:
        raise HTTPException(status_code=422, detail={"reason": "workspace_id_required"})

    task = Task(
        company_id=company_id,
        created_by=user.id,
        slug=payload.slug,
        title=payload.title,
        description_md=payload.description_md,
        starter_files=[f.model_dump() for f in payload.starter_files],
        runtime_image=payload.runtime_image,
        weakness=payload.weakness,
        spec=payload.spec,
        visibility=payload.visibility,
        is_template=False,
    )
    db.add(task)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"reason": "slug_taken", "slug": payload.slug},
        )
    await db.commit()
    await db.refresh(task)
    logger.info("task created: id=%s slug=%s company=%s", task.id, task.slug, company_id)
    return _to_response(task, can_edit=True)


@router.put("/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: uuid.UUID,
    payload: TaskUpdate,
    db: AsyncSession = Depends(session_scope),
    user: User = Depends(current_user),
) -> TaskResponse:
    task, can_edit = await _load_visible_task(db, task_id, user)
    if not can_edit:
        raise HTTPException(status_code=403, detail={"reason": "read_only"})

    # exclude_none too: every TaskUpdate field maps to a NOT-NULL column, so an
    # explicit JSON null means "no change", never "set null" (which would 500 on
    # commit). exclude_unset already drops omitted fields; this also drops nulls.
    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    if "starter_files" in data:
        data["starter_files"] = [f.model_dump() for f in payload.starter_files or []]
    for field, value in data.items():
        setattr(task, field, value)

    await db.commit()
    await db.refresh(task)
    logger.info("task updated: id=%s slug=%s", task.id, task.slug)
    return _to_response(task, can_edit=True)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(session_scope),
    user: User = Depends(current_user),
) -> None:
    task, can_edit = await _load_visible_task(db, task_id, user)
    if not can_edit:
        raise HTTPException(status_code=403, detail={"reason": "read_only"})
    await db.delete(task)
    await db.commit()
    logger.info("task deleted: id=%s slug=%s", task_id, task.slug)
