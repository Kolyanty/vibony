"""0014 share-token expiry: add sessions.share_token_expires_at.

The replay share_token had no expiry and no revoke. It is now minted only for
completed sessions, stamps an expiry (now + SHARE_TOKEN_TTL_DAYS) at mint, and
adds a DELETE /sessions/{code}/share revoke (which nulls both columns). The
share-token gates (replay config + share-scoped events) treat a past expiry the
same as an invalid token — no leak.

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column(
            "share_token_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("sessions", "share_token_expires_at")
