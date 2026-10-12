"""Portable task-artifact importer.

A **task artifact** is a self-contained directory — the decoupling boundary between
a (future) generator that *produces* it, this importer that *loads* it into a
``tasks`` row, and the trap-bench that *validates* it. The format is the existing
``tasks/<id>/`` layout, formalized: ``spec.yaml`` is the manifest + source of truth
(it carries ``id`` / ``runtime_image`` / ``format_version`` / the trap contracts),
``task.md`` is the candidate projection, ``weakness.yaml`` is the AI config, the
remaining non-private top-level files are the starter scaffold, and ``_*`` files are
private (reference solution etc.). Full format spec: ``docs/task-design/format.md`` §7.

Two layers:

* :func:`read_artifact` — a pure filesystem read of one artifact directory into
  :class:`ArtifactData` (the same projection the seed produced, plus
  ``format_version``). No DB, no validation, no raises on a non-task dir (``None``).
* :func:`import_artifact` — validate (the shared field rules) + idempotent
  upsert into a ``tasks`` row, returning the row. The template seed
  (:mod:`vibony_backend.tasks.seed`) is one caller (persistent, ``is_template``);
  the trap-bench is another (ephemeral staging of an arbitrary artifact).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from vibony_backend.db.models import Task
from vibony_backend.sessions.bundle import _is_private
from vibony_backend.tasks.images import DEFAULT_TEMPLATE_IMAGE
from vibony_backend.tasks.schemas import validate_task_artifact_fields

logger = logging.getLogger(__name__)

# Current portable-artifact format version. Stored inside the ``spec`` JSONB (not a
# dedicated column — additive, no migration), surfaced on :class:`ArtifactData`.
# Bump when the artifact layout changes in a way an importer must branch on.
ARTIFACT_FORMAT_VERSION = 1

# Spec keys promoted to dedicated columns — stripped from the stored ``spec`` JSONB.
# ``format_version`` deliberately stays IN the spec JSONB (round-trips on re-import).
_SPEC_COLUMN_KEYS = frozenset({"id", "runtime_image"})


class ArtifactError(ValueError):
    """An artifact directory is not importable (missing/invalid required content)."""


@dataclass(frozen=True)
class ArtifactData:
    """Pure projection of an artifact directory into ``tasks`` column values."""

    slug: str
    title: str
    description_md: str
    starter_files: list[dict[str, str]]
    runtime_image: str
    weakness: dict[str, Any]
    spec: dict[str, Any]
    format_version: int | None


def _safe_load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("artifact: failed to parse %s: %s", path, exc)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _derive_title(description_md: str, slug: str) -> str:
    """First markdown H1 from task.md, else a title-cased slug."""
    for line in description_md.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return slug.replace("-", " ").strip().title()


def read_artifact(artifact_dir: Path) -> ArtifactData | None:
    """Read an artifact directory into :class:`ArtifactData` (pure filesystem read).

    Returns ``None`` when the dir is not a task (no ``weakness.yaml`` — the same
    gate the seed used). ``task.md`` becomes ``description_md``; the remaining
    non-private, UTF-8 top-level files become ``starter_files``; ``spec.yaml``'s
    ``runtime_image`` becomes the column value (falling back to the default image),
    the rest of the spec (minus ``id`` / ``runtime_image``) is the ``spec`` JSONB;
    ``weakness.yaml``'s inner ``weakness:`` mapping becomes ``weakness``.
    """
    if not (artifact_dir / "weakness.yaml").is_file():
        return None

    slug = artifact_dir.name
    description_md = ""
    task_md = artifact_dir / "task.md"
    if task_md.is_file():
        description_md = task_md.read_text(encoding="utf-8")

    starter_files: list[dict[str, str]] = []
    for p in sorted(artifact_dir.iterdir(), key=lambda x: x.name):
        if not p.is_file() or _is_private(p.name) or p.name == "task.md":
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            logger.warning("artifact: skipping non-UTF-8 starter file %s", p)
            continue
        starter_files.append({"path": p.name, "content": content})

    weakness = _safe_load(artifact_dir / "weakness.yaml").get("weakness") or {}

    spec_raw = _safe_load(artifact_dir / "spec.yaml")
    runtime_image = spec_raw.get("runtime_image")
    if not (isinstance(runtime_image, str) and runtime_image.strip()):
        runtime_image = DEFAULT_TEMPLATE_IMAGE
    fmt = spec_raw.get("format_version")
    spec = {k: v for k, v in spec_raw.items() if k not in _SPEC_COLUMN_KEYS}

    return ArtifactData(
        slug=slug,
        title=_derive_title(description_md, slug),
        description_md=description_md,
        starter_files=starter_files,
        runtime_image=runtime_image.strip(),
        weakness=weakness if isinstance(weakness, dict) else {},
        spec=spec,
        format_version=fmt if isinstance(fmt, int) else None,
    )


def _content_columns(
    data: ArtifactData, *, visibility: str, is_template: bool
) -> dict[str, Any]:
    """The Task content columns an upsert sets (everything but identity keys)."""
    return {
        "title": data.title,
        "description_md": data.description_md,
        "starter_files": data.starter_files,
        "runtime_image": data.runtime_image,
        "weakness": data.weakness,
        "spec": data.spec,
        "visibility": visibility,
        "is_template": is_template,
    }


async def import_artifact(
    db: AsyncSession,
    artifact_dir: Path,
    *,
    company_id: uuid.UUID,
    created_by: uuid.UUID | None = None,
    is_template: bool = False,
    visibility: str = "workspace",
    slug_override: str | None = None,
) -> Task:
    """Import one artifact directory into a ``tasks`` row (validated, idempotent).

    Reads the artifact, re-applies the field rules
    (:func:`validate_task_artifact_fields` — slug shape, image whitelist, bare
    non-private starter-file names + size caps, JSONB size), then upserts by
    ``(company_id, slug)`` (force-restoring every content column on conflict) and
    returns the row. The caller owns the transaction (no commit here).

    ``slug_override`` replaces the directory-derived slug — used by the trap-bench
    to stage an arbitrary artifact under a throwaway, collision-free slug. Raises
    :class:`ArtifactError` when the directory is not a task, or ``ValueError`` (from
    the field validators) when its content is invalid.
    """
    data = read_artifact(artifact_dir)
    if data is None:
        raise ArtifactError(f"{artifact_dir} is not a task (no weakness.yaml)")
    slug = slug_override or data.slug

    validate_task_artifact_fields(
        slug=slug,
        runtime_image=data.runtime_image,
        starter_files=data.starter_files,
        weakness=data.weakness,
        spec=data.spec,
    )

    content = _content_columns(data, visibility=visibility, is_template=is_template)
    await db.execute(
        pg_insert(Task)
        .values(
            id=uuid.uuid4(),
            company_id=company_id,
            created_by=created_by,
            slug=slug,
            **content,
        )
        .on_conflict_do_update(
            index_elements=["company_id", "slug"],
            set_={**content, "updated_at": func.now()},
        )
    )
    row = (
        await db.execute(
            select(Task).where(Task.company_id == company_id, Task.slug == slug)
        )
    ).scalar_one()
    return row


__all__ = [
    "ARTIFACT_FORMAT_VERSION",
    "ArtifactData",
    "ArtifactError",
    "import_artifact",
    "read_artifact",
]
