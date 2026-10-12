"""0002 share_token: add share_token column to sessions.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("share_token", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sessions", "share_token")
