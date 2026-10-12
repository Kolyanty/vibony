"""0004 session_runtime: add sessions.runtime column.

Adds `runtime TEXT NOT NULL DEFAULT 'cli'` to the sessions table.
Existing rows get the default 'cli', keeping the CLI flow intact.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column(
            "runtime",
            sa.Text(),
            nullable=False,
            server_default="cli",
        ),
    )


def downgrade() -> None:
    op.drop_column("sessions", "runtime")
