"""0006 async_mode: add mode + time_limit_seconds + started_at.

Adds the columns needed to create a session as **async** (timer, no interviewer)
vs the existing **live** mode:
- `mode TEXT NOT NULL DEFAULT 'live'` — interview mode. Existing rows become 'live'.
- `time_limit_seconds INTEGER NULL` — async deadline window (NULL for live).
- `started_at TIMESTAMPTZ NULL` — set when the candidate first connects; the
  anchor for the server-side async deadline (`started_at + time_limit_seconds`).
  This migration only adds the column.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column(
            "mode",
            sa.Text(),
            nullable=False,
            server_default="live",
        ),
    )
    op.add_column(
        "sessions",
        sa.Column("time_limit_seconds", sa.Integer(), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sessions", "started_at")
    op.drop_column("sessions", "time_limit_seconds")
    op.drop_column("sessions", "mode")
