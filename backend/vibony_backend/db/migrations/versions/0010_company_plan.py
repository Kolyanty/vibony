"""0010 company plan: add companies.plan + trial_ends_at.

Introduces a billing-plan dimension as a stub for future billing: every
workspace starts on a `trial` plan with a `trial_ends_at` countdown. No billing
logic yet — the column exists so billing can build on it and the dashboard can show
a trial badge + an upgrade placeholder.

- `plan` — `trial` (default) | `pro` (reserved). Server-default `trial`, so existing
  rows backfill to trial.
- `trial_ends_at` — NULL for existing rows (no countdown shown); set to now + 14d for
  new workspaces.

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("plan", sa.Text(), nullable=False, server_default="trial"),
    )
    op.add_column(
        "companies",
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("companies", "trial_ends_at")
    op.drop_column("companies", "plan")
