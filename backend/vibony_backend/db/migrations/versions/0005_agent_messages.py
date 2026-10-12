"""0005 agent_messages: add sessions.agent_messages column.

Adds `agent_messages JSONB NULL` to the sessions table — the persisted live LLM
message blob (AgentHost._messages), restored verbatim on cold-start so tool-call
rounds survive a backend restart / tab reopen. Distinct from the event log, which
stays canonical for the dashboard/replay. Existing rows get NULL and fall back to
the legacy user/assistant event replay.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column(
            "agent_messages",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("sessions", "agent_messages")
