"""0009 tasks: add tasks table + sessions.task_ref_id.

Moves interview tasks from the filesystem (`tasks/<id>/`) into the DB. This
migration adds the storage; the runtime rewire + template seed land later.

- `tasks` — one row per authored (or seeded template) task:
  - `company_id` FK companies CASCADE — owner workspace (templates: the system
    "Vibony Templates" company, seeded at lifespan startup).
  - `created_by` FK users SET NULL — author (NULL for templates).
  - `slug` — human id, unique per company; for templates == the repo dir name.
  - `description_md` — candidate-facing `task.md`.
  - `starter_files` JSONB — `list[{path, content}]` (text scaffold files).
  - `runtime_image` — sandbox image (validated vs ALLOWED_RUNTIME_IMAGES on save).
  - `weakness` / `spec` JSONB — the parsed weakness + assessment-spec mappings
    (fed verbatim to the existing pure renderers/parsers).
  - `visibility` — private | workspace | public (public stored, UI deferred).
  - `is_template` — built-in reference task surfaced read-only to every workspace.
- `sessions.task_ref_id` FK tasks SET NULL — integrity link + cross-workspace
  resolver. `sessions.task_id` (slug string) is kept for protocol stability;
  the slug→ref backfill happens in the seed.

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description_md", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "starter_files",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("runtime_image", sa.Text(), nullable=False),
        sa.Column(
            "weakness",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "spec",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "visibility", sa.Text(), nullable=False, server_default="workspace"
        ),
        sa.Column(
            "is_template",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "slug", name="uq_tasks_company_slug"),
    )

    op.add_column(
        "sessions",
        sa.Column("task_ref_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_sessions_task_ref_id",
        "sessions",
        "tasks",
        ["task_ref_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_sessions_task_ref_id", "sessions", type_="foreignkey")
    op.drop_column("sessions", "task_ref_id")
    op.drop_table("tasks")
