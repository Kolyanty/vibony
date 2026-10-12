"""0012 audit log: add audit_log table.

Minimal append-only activity log: one row per recorded action
(session created, workspace created, member invited/joined/role-changed/removed,
invite revoked). Admin-only `GET /workspaces/{cid}/audit` reads it most-recent-first.

- `company_id` FK companies CASCADE — the workspace the action belongs to.
- `actor_user_id` FK users SET NULL — who did it (NULL for system/after user delete).
- `action` — dotted string (`session.created`, `member.role_changed`, …).
- `target_type` / `target_id` — optional subject of the action (text, schema-free).
- `metadata` JSONB — small action-specific context (email, role, session_code).

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=True),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_log_company_created_desc",
        "audit_log",
        ["company_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_company_created_desc", table_name="audit_log")
    op.drop_table("audit_log")
