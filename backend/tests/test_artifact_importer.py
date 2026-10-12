"""Portable task-artifact importer.

`read_artifact` is a pure filesystem read of an artifact directory; `import_artifact`
validates (the shared field rules) + idempotently upserts a `tasks` row. The
template seed (test_task_seed.py) is one caller; the trap-bench staging is another.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vibony_backend.assessment.inputs import parse_task_spec
from vibony_backend.db.models import Company, Task
from vibony_backend.tasks.images import DEFAULT_TEMPLATE_IMAGE
from vibony_backend.tasks.importer import (
    ArtifactError,
    import_artifact,
    read_artifact,
)
from vibony_backend.tasks.schemas import validate_task_artifact_fields

_session_loop = pytest.mark.asyncio(loop_scope="session")

_REPO_TASKS = Path(__file__).resolve().parents[2] / "tasks"


def _write_artifact(
    root: Path,
    *,
    slug: str = "task-99-demo",
    runtime_image: str | None = "vibony/sandbox-py:3.12",
    format_version: int | None = 1,
    trap_type: str | None = "concurrency/race",
    starter: dict[str, str] | None = None,
) -> Path:
    """Build a minimal valid artifact directory under ``root`` and return it."""
    d = root / slug
    d.mkdir()
    spec_lines = [f"id: {slug}", "discipline: backend"]
    if format_version is not None:
        spec_lines.append(f"format_version: {format_version}")
    if runtime_image is not None:
        spec_lines.append(f"runtime_image: {runtime_image}")
    spec_lines += [
        "context: a demo task",
        "expected_ai_pitfalls:",
        "  - description: AI forgets the guard",
        "    flawed_behavior: omits the check",
    ]
    if trap_type is not None:
        spec_lines.append(f"    trap_type: {trap_type}")
    (d / "spec.yaml").write_text("\n".join(spec_lines) + "\n")
    (d / "task.md").write_text("# Demo Task\n\nDo the thing.\n")
    (d / "weakness.yaml").write_text("weakness:\n  blindspots:\n    - be terse\n")
    for name, content in (starter or {"app.py": "x = 1\n"}).items():
        (d / name).write_text(content)
    return d


# --- read_artifact (pure filesystem) ---


def test_read_artifact_training_loop_has_format_version():
    data = read_artifact(_REPO_TASKS / "task-01-training-loop")
    assert data is not None
    assert data.slug == "task-01-training-loop"
    assert data.runtime_image == "vibony/sandbox-py:3.12"
    assert data.format_version == 1  # declared in spec.yaml (worked example)
    paths = {f["path"] for f in data.starter_files}
    assert {"train.py", "model.py", "data.py", "pyproject.toml"} <= paths
    assert "task.md" not in paths and "spec.yaml" not in paths
    assert "_solution.py" not in paths and "weakness.yaml" not in paths
    # Column-promoted keys stripped; format_version stays IN the spec JSONB.
    assert "runtime_image" not in data.spec and "id" not in data.spec
    assert data.spec["format_version"] == 1
    assert data.spec["expected_ai_pitfalls"]


def test_read_artifact_no_spec_defaults(tmp_path: Path):
    artifact = _write_artifact(tmp_path)
    (artifact / "spec.yaml").unlink()
    data = read_artifact(artifact)
    assert data is not None
    assert data.runtime_image == DEFAULT_TEMPLATE_IMAGE
    assert data.format_version is None  # absent spec has no format version
    assert data.spec == {}


def test_read_artifact_non_task_returns_none(tmp_path: Path):
    d = tmp_path / "not-a-task"
    d.mkdir()
    (d / "readme.txt").write_text("hi")
    assert read_artifact(d) is None  # no weakness.yaml


def test_read_artifact_trap_type_round_trips(tmp_path: Path):
    d = _write_artifact(tmp_path, trap_type="missing-check")
    data = read_artifact(d)
    assert data is not None and data.format_version == 1
    # trap_type lives inside the pitfall in the spec JSONB and is read by parse_task_spec.
    spec = parse_task_spec(data.slug, {**data.spec, "id": data.slug})
    assert spec is not None
    assert spec.expected_ai_pitfalls[0].trap_type == "missing-check"


# --- validate_task_artifact_fields (pure) ---


def test_validate_rejects_private_starter_name():
    with pytest.raises(ValueError):
        validate_task_artifact_fields(
            slug="task-99-demo",
            runtime_image="vibony/sandbox-py:3.12",
            starter_files=[{"path": "weakness.yaml", "content": "x"}],
            weakness={},
            spec={},
        )


def test_validate_rejects_too_many_starters():
    with pytest.raises(ValueError):
        validate_task_artifact_fields(
            slug="task-99-demo",
            runtime_image="vibony/sandbox-py:3.12",
            starter_files=[{"path": f"f{i}.py", "content": ""} for i in range(101)],
            weakness={},
            spec={},
        )


# --- import_artifact (DB upsert) ---


async def _company(db: AsyncSession) -> uuid.UUID:
    c = Company(name="Importer Co")
    db.add(c)
    await db.flush()
    return c.id


@_session_loop
async def test_import_artifact_creates_row(db_session: AsyncSession):
    cid = await _company(db_session)
    row = await import_artifact(
        db_session,
        _REPO_TASKS / "task-01-training-loop",
        company_id=cid,
        visibility="workspace",
    )
    assert row.company_id == cid
    assert row.slug == "task-01-training-loop"
    assert row.runtime_image == "vibony/sandbox-py:3.12"
    assert row.is_template is False and row.visibility == "workspace"
    assert "runtime_image" not in row.spec and "id" not in row.spec
    assert row.spec["expected_ai_pitfalls"]


@_session_loop
async def test_import_artifact_idempotent(db_session: AsyncSession):
    cid = await _company(db_session)
    d = _REPO_TASKS / "task-01-training-loop"
    a = await import_artifact(db_session, d, company_id=cid)
    b = await import_artifact(db_session, d, company_id=cid)
    assert a.id == b.id  # upsert by (company_id, slug), not a duplicate
    rows = (
        await db_session.execute(select(Task).where(Task.company_id == cid))
    ).scalars().all()
    assert len(rows) == 1


@_session_loop
async def test_import_artifact_slug_override(db_session: AsyncSession):
    cid = await _company(db_session)
    row = await import_artifact(
        db_session,
        _REPO_TASKS / "task-01-training-loop",
        company_id=cid,
        slug_override="task-01-training-loop-tvabc123",
    )
    assert row.slug == "task-01-training-loop-tvabc123"


@_session_loop
async def test_import_artifact_rejects_bad_image(db_session: AsyncSession, tmp_path: Path):
    cid = await _company(db_session)
    d = _write_artifact(tmp_path, runtime_image="evil/image:latest")
    with pytest.raises(ValueError):
        await import_artifact(db_session, d, company_id=cid)


@_session_loop
async def test_import_artifact_rejects_bad_slug(db_session: AsyncSession, tmp_path: Path):
    cid = await _company(db_session)
    d = _write_artifact(tmp_path)
    with pytest.raises(ValueError):
        await import_artifact(
            db_session, d, company_id=cid, slug_override="Not A Slug!"
        )


@_session_loop
async def test_import_artifact_non_task_raises(db_session: AsyncSession, tmp_path: Path):
    cid = await _company(db_session)
    d = tmp_path / "empty"
    d.mkdir()
    with pytest.raises(ArtifactError):
        await import_artifact(db_session, d, company_id=cid)
