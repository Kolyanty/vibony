"""Task CRUD DTOs.

Maps the `tasks` table to the dashboard. Visibility is private / workspace /
public, but `public` is not creatable via the API (cross-workspace
listing deferred) — `TaskCreate`/`TaskUpdate` accept only private/workspace.
The weakness + assessment-spec mappings are free-form JSONB (the dashboard
edits them as raw YAML); only their top-level shape (a mapping) is validated here.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from vibony_backend.sessions.bundle import _is_private
from vibony_backend.tasks.images import ALLOWED_RUNTIME_IMAGES

Visibility = Literal["private", "workspace", "public"]
# Visibility a user may set via the API (public is template-only).
EditableVisibility = Literal["private", "workspace"]

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# Input caps: authored task fields are stored verbatim and re-parsed
# every session start, so bound them. Body-size middleware is the gross backstop;
# these give a clear per-field 422.
MAX_STARTER_FILES = 100
MAX_FILE_CONTENT = 256 * 1024  # 256 KB per starter file
MAX_DESCRIPTION = 64 * 1024
MAX_TITLE = 200
MAX_JSONB_BYTES = 256 * 1024  # serialized weakness / spec mapping


def _check_jsonb_size(v: dict[str, Any] | None) -> dict[str, Any] | None:
    if v is not None and len(json.dumps(v)) > MAX_JSONB_BYTES:
        raise ValueError(f"mapping exceeds {MAX_JSONB_BYTES // 1024} KB serialized")
    return v


class StarterFile(BaseModel):
    path: str = Field(..., max_length=256)
    content: str = Field(default="", max_length=MAX_FILE_CONTENT)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        name = v.strip()
        if not name:
            raise ValueError("starter file path must be non-empty")
        if "/" in name or "\\" in name or name.startswith("."):
            raise ValueError(
                f"starter file path must be a bare top-level filename: {v!r}"
            )
        if _is_private(name):
            raise ValueError(
                f"{name!r} is a reserved/private filename and cannot be a candidate file"
            )
        return name


def _validate_image(v: str) -> str:
    if v not in ALLOWED_RUNTIME_IMAGES:
        raise ValueError(
            f"runtime_image must be one of {list(ALLOWED_RUNTIME_IMAGES)}"
        )
    return v


def _validate_slug(v: str) -> str:
    if not _SLUG_RE.match(v):
        raise ValueError(
            "slug must be lowercase alphanumeric + hyphens, 1–64 chars, "
            "starting with a letter or digit"
        )
    return v


def validate_task_artifact_fields(
    *,
    slug: str,
    runtime_image: str,
    starter_files: list[dict[str, Any]],
    weakness: dict[str, Any] | None,
    spec: dict[str, Any] | None,
) -> None:
    """Re-apply the field rules to importer-supplied task content.

    The portable-artifact importer (`tasks/importer.py`) does not go through the
    create-only `TaskCreate` schema (which forbids `public` visibility — templates
    are public), so the shared field rules live here as a standalone gate the
    importer calls: slug shape, the runtime-image whitelist, the bare/non-private
    starter-file names + per-file size cap, and the JSONB size caps. Raises
    ``ValueError`` (mirroring the pydantic validators) on the first failure.
    """
    _validate_slug(slug)
    _validate_image(runtime_image)
    if len(starter_files) > MAX_STARTER_FILES:
        raise ValueError(f"too many starter files (max {MAX_STARTER_FILES})")
    for entry in starter_files:
        # StarterFile's validators enforce the bare-top-level / non-private name
        # rule + the per-file content cap — reuse them so there is one source of truth.
        StarterFile(**entry)
    _check_jsonb_size(weakness)
    _check_jsonb_size(spec)


class TaskCreate(BaseModel):
    slug: str
    title: str = Field(..., min_length=1, max_length=MAX_TITLE)
    description_md: str = Field(default="", max_length=MAX_DESCRIPTION)
    starter_files: list[StarterFile] = Field(default_factory=list, max_length=MAX_STARTER_FILES)
    runtime_image: str
    weakness: dict[str, Any] = Field(default_factory=dict)
    spec: dict[str, Any] = Field(default_factory=dict)
    visibility: EditableVisibility = "workspace"
    workspace_id: uuid.UUID | None = None

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, v: str) -> str:
        return _validate_slug(v)

    @field_validator("runtime_image")
    @classmethod
    def _check_image(cls, v: str) -> str:
        return _validate_image(v)

    @field_validator("weakness", "spec")
    @classmethod
    def _check_jsonb(cls, v: dict[str, Any]) -> dict[str, Any]:
        return _check_jsonb_size(v)


class TaskUpdate(BaseModel):
    """Partial update — every field optional; slug + workspace are immutable."""

    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE)
    description_md: str | None = Field(default=None, max_length=MAX_DESCRIPTION)
    starter_files: list[StarterFile] | None = Field(default=None, max_length=MAX_STARTER_FILES)
    runtime_image: str | None = None
    weakness: dict[str, Any] | None = None
    spec: dict[str, Any] | None = None
    visibility: EditableVisibility | None = None

    @field_validator("runtime_image")
    @classmethod
    def _check_image(cls, v: str | None) -> str | None:
        return v if v is None else _validate_image(v)

    @field_validator("weakness", "spec")
    @classmethod
    def _check_jsonb(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        return _check_jsonb_size(v)


class TaskListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    slug: str
    title: str
    runtime_image: str
    visibility: str
    is_template: bool
    can_edit: bool = False  # set per-request by the endpoint
    is_favorite: bool = False  # set per-request by the endpoint (current user's favorite)
    created_at: datetime
    updated_at: datetime


class TaskResponse(TaskListItem):
    created_by: uuid.UUID | None = None
    description_md: str
    starter_files: list[StarterFile]
    weakness: dict[str, Any]
    spec: dict[str, Any]


class TaskListResponse(BaseModel):
    tasks: list[TaskListItem]
