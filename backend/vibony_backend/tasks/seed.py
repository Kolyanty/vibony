"""Template task seed — import the repo reference tasks into the DB.

The reference tasks (task-01, task-02) live as files in the repo. This module
imports them into `is_template=True` rows owned by the fixed **"Vibony Templates"**
system company, surfaced read-only to every workspace. Idempotent: re-run on each
backend startup (upsert by `(company_id, slug)`), so editing the repo files
re-syncs the templates on the next deploy. Also backfills `Session.task_ref_id`
for older rows by matching the `task_id` slug.

The actual read + validate + upsert is the generalized portable-artifact
importer (:mod:`vibony_backend.tasks.importer`); this module is just the
repo-template *caller* of it (plus the system-company bootstrap + the backfill).

Wired into `main.py` lifespan via :func:`run_template_seed` (opens its own session
+ commits). Tests call :func:`seed_template_tasks` with the rolled-back fixture
session for isolation.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from vibony_backend.db.engine import get_sessionmaker
from vibony_backend.db.models import Company, Task
from vibony_backend.tasks.importer import import_artifact, read_artifact

logger = logging.getLogger(__name__)

# Fixed identity for the system templates workspace. Well-known so seeding is
# idempotent across deploys and the access layer can special-case templates.
SYSTEM_COMPANY_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
SYSTEM_COMPANY_NAME = "Vibony Templates"
SYSTEM_COMPANY_SLUG = "vibony-templates"

# seed.py is backend/vibony_backend/tasks/seed.py → parents[3] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_TASKS_DIR = _REPO_ROOT / "tasks"


def import_task_from_dir(task_dir: Path) -> dict[str, Any] | None:
    """Back-compat adapter over :func:`importer.read_artifact`.

    Returns the legacy column-value dict (or ``None`` for a non-task dir) that
    older callers + tests expect. New code should use ``read_artifact`` /
    ``import_artifact`` directly.
    """
    data = read_artifact(task_dir)
    if data is None:
        return None
    return {
        "slug": data.slug,
        "title": data.title,
        "description_md": data.description_md,
        "starter_files": data.starter_files,
        "runtime_image": data.runtime_image,
        "weakness": data.weakness,
        "spec": data.spec,
    }


async def ensure_system_company(db: AsyncSession) -> None:
    await db.execute(
        pg_insert(Company)
        .values(
            id=SYSTEM_COMPANY_ID,
            name=SYSTEM_COMPANY_NAME,
            slug=SYSTEM_COMPANY_SLUG,
        )
        .on_conflict_do_nothing()
    )


async def seed_template_tasks(db: AsyncSession) -> int:
    """Upsert every repo reference task as a template + backfill sessions.

    Caller owns the transaction (no commit here). Returns the number of template
    tasks upserted. Each dir goes through :func:`importer.import_artifact` —
    templates are repo-owned + read-only in the UI, so the upsert
    force-restores every content field (incl. visibility/is_template) from the repo
    on each deploy. There is no product path that hand-edits a template, so this is
    a deliberate re-sync, not a lost-write hazard.
    """
    await ensure_system_company(db)

    count = 0
    current_slugs: set[str] = set()
    if _TASKS_DIR.is_dir():
        for task_dir in sorted(_TASKS_DIR.iterdir(), key=lambda x: x.name):
            if not task_dir.is_dir():
                continue
            if not (task_dir / "weakness.yaml").is_file():
                continue  # not a task (mirrors read_artifact's gate)
            await import_artifact(
                db,
                task_dir,
                company_id=SYSTEM_COMPANY_ID,
                created_by=None,
                is_template=True,
                visibility="public",
            )
            current_slugs.add(task_dir.name)
            count += 1

    # Backfill older sessions (and any with a dropped ref) by slug.
    # Prefer the session's OWN workspace task over a same-slug template —
    # the old query joined only on t.company_id = SYSTEM, so a session in workspace W
    # whose slug also exists as W's private/workspace task got mis-linked to the
    # template. DISTINCT ON picks the workspace task first (is_template ASC), mirroring
    # find_task_by_slug's precedence; a legacy NULL-company session matches only the
    # template (the company_id equality is NULL-false), so it still links to the template.
    await db.execute(
        text(
            "UPDATE sessions SET task_ref_id = chosen.task_id "
            "FROM ("
            "  SELECT DISTINCT ON (s.id) s.id AS session_id, t.id AS task_id "
            "  FROM sessions s "
            "  JOIN tasks t ON t.slug = s.task_id "
            "    AND (t.company_id = s.company_id OR t.is_template) "
            "  WHERE s.task_ref_id IS NULL "
            "  ORDER BY s.id, t.is_template ASC"
            ") AS chosen "
            "WHERE sessions.id = chosen.session_id"
        )
    )

    # Retire removed/renamed system templates after backfilling existing sessions.
    # Keep the rows and their content for replay and sessions that have not started;
    # workspace-authored tasks and copies are outside this synchronization boundary.
    # A missing checkout must not silently retire the entire catalog.
    if current_slugs:
        await db.execute(
            update(Task)
            .where(
                Task.company_id == SYSTEM_COMPANY_ID,
                Task.is_template.is_(True),
                Task.slug.not_in(current_slugs),
            )
            .values(is_template=False, visibility="private")
        )

    logger.info("seed_template_tasks: upserted %d template task(s)", count)
    return count


async def run_template_seed() -> int:
    """Lifespan entry: open a session, seed + backfill, commit. Never raises."""
    sm = get_sessionmaker()
    async with sm() as db:
        async with db.begin():
            return await seed_template_tasks(db)


__all__ = [
    "SYSTEM_COMPANY_ID",
    "SYSTEM_COMPANY_NAME",
    "SYSTEM_COMPANY_SLUG",
    "ensure_system_company",
    "import_task_from_dir",
    "run_template_seed",
    "seed_template_tasks",
]
