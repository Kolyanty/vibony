"""0015 task favorites: add task_favorites table (dashboard task-picker UX).

Per-user favorite tasks — a (user, task) pair surfaced first in the task picker
and catalog. Personal, no workspace/permission model. Both FKs cascade so
deleting a user or a task drops the favorite rows. Unique per (user, task) so a
PUT /tasks/{id}/favorite is idempotent.

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_favorites",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "task_id", name="uq_task_favorites_user_task"),
    )
    op.create_index("ix_task_favorites_user", "task_favorites", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_task_favorites_user", table_name="task_favorites")
    op.drop_table("task_favorites")
