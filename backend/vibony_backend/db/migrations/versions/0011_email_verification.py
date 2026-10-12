"""0011 email verification: add users.email_verified_at.

Email verification is non-blocking: signup still logs the user in immediately;
an unverified user just sees a dashboard banner. The verification token is stateless
(itsdangerous-signed user id), so the only schema addition is the timestamp that
records when (if) the email was confirmed. NULL = unverified.

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "email_verified_at")
