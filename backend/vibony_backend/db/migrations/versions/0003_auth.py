"""0003 auth: add auth tables (companies, users, oauth_identities, memberships, invites)
and update sessions (owner_token → candidate_token; add company_id, created_by).

company_id / created_by are nullable in this migration; NOT NULL will be
enforced in 0004 when auth is wired to the REST endpoints.

No data migration: we DELETE all existing events and sessions first
so the new nullable columns are added to a clean table.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop all pre-auth rows before adding new columns / tables.
    op.execute("DELETE FROM events")
    op.execute("DELETE FROM sessions")

    # --- New auth tables (FK dependency order) ---

    op.create_table(
        "companies",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_companies_slug"),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    op.create_table(
        "oauth_identities",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("provider", "subject"),
    )
    op.create_index(
        "ix_oauth_identities_user_id", "oauth_identities", ["user_id"]
    )

    op.create_table(
        "memberships",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "company_id"),
    )

    op.create_table(
        "invites",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("invited_by", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["accepted_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token", name="uq_invites_token"),
    )

    # --- Update sessions table ---

    op.add_column(
        "sessions",
        sa.Column("company_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("created_by", sa.UUID(), nullable=True),
    )
    # Table is empty after the DELETE above, so NOT NULL without DEFAULT is fine.
    op.add_column(
        "sessions",
        sa.Column("candidate_token", sa.Text(), nullable=False),
    )

    op.create_foreign_key(
        "fk_sessions_company_id",
        "sessions",
        "companies",
        ["company_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_sessions_created_by",
        "sessions",
        "users",
        ["created_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_sessions_candidate_token", "sessions", ["candidate_token"]
    )
    op.execute(
        "CREATE INDEX ix_sessions_company_created_desc ON sessions (company_id, created_at DESC)"
    )

    op.drop_column("sessions", "owner_token")


def downgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("owner_token", sa.Text(), nullable=True),
    )
    op.execute("DROP INDEX IF EXISTS ix_sessions_company_created_desc")
    op.drop_constraint(
        "uq_sessions_candidate_token", "sessions", type_="unique"
    )
    op.drop_constraint("fk_sessions_created_by", "sessions", type_="foreignkey")
    op.drop_constraint("fk_sessions_company_id", "sessions", type_="foreignkey")
    op.drop_column("sessions", "candidate_token")
    op.drop_column("sessions", "created_by")
    op.drop_column("sessions", "company_id")

    op.drop_table("invites")
    op.drop_table("memberships")
    op.drop_index("ix_oauth_identities_user_id", table_name="oauth_identities")
    op.drop_table("oauth_identities")
    op.drop_table("users")
    op.drop_table("companies")
