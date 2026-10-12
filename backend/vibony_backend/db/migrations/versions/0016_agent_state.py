"""Persist private agent checkpoints and role-scoped LLM usage."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("agent_state", JSONB(), nullable=True))
    op.add_column("sessions", sa.Column("llm_usage", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "llm_usage")
    op.drop_column("sessions", "agent_state")
