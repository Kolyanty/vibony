"""0008 drop_session_runtime: remove sessions.runtime column.

The legacy CLI candidate runtime was archived; the browser IDE (web) is
the only candidate runtime. The `runtime` column (added in 0004) is now a dead
flag — every session is web — so we drop it along with the CLI WS endpoint,
FsProxy and the `RuntimeMode` schema field.

Downgrade re-adds the column with the original `NOT NULL DEFAULT 'cli'` so the
schema is restorable, but no code reads it anymore.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("sessions", "runtime")


def downgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column(
            "runtime",
            sa.Text(),
            nullable=False,
            server_default="cli",
        ),
    )
