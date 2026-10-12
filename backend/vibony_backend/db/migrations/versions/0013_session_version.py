"""0013 session revocation: add users.session_version.

The session cookie embeds the user's session_version; current_user / the
interviewer WS reject a cookie whose embedded version != the stored one. Bumping
session_version (on logout, and on password change when that lands) invalidates
every outstanding cookie server-side — closing the "logout only clears the local
cookie, a captured token stays valid 14 days" gap.

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "session_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "session_version")
