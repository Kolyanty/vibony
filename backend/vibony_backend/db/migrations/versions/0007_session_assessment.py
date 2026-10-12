"""0007 session_assessment: add session_assessments table.

Storage for the AI assessment artifact: one current row per session
(`session_id` UNIQUE), overwritten on regenerate — no history table yet.

- `status TEXT NOT NULL` — job state: pending / running / ready / failed.
- `model TEXT NULL` — which model produced the report (set on ready).
- `summary_md TEXT NULL` — the prose sections (set on ready).
- `findings JSONB NOT NULL DEFAULT '[]'` — structured findings list.
- `error TEXT NULL` — failure detail (set on failed).
- `created_at` / `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()` — updated_at is
  bumped via the ORM `onupdate` (matches the `users` table; no DB trigger).

FK session_id → sessions.id ON DELETE CASCADE; UNIQUE(session_id) makes it 1:1.
No behaviour yet (schema only); enqueue/generation land later.

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "session_assessments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("summary_md", sa.Text(), nullable=True),
        sa.Column(
            "findings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", name="uq_session_assessments_session_id"),
    )


def downgrade() -> None:
    op.drop_table("session_assessments")
