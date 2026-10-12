"""SQLAlchemy 2.0 models: Session, Event + auth tables.

Auth tables: Company, User, OAuthIdentity, Membership, Invite.
Session.owner_token → Session.candidate_token (opaque uuid4).
Session.company_id / created_by are nullable initially; NOT NULL enforced later.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    # Billing-plan stub. 'trial' (default) | 'pro' (reserved). No billing
    # logic yet — the dimension exists for the dashboard trial badge.
    plan: Mapped[str] = mapped_column(Text, nullable=False, server_default="trial")
    trial_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    members: Mapped[list[Membership]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    sessions: Mapped[list[Session]] = relationship(back_populates="company")
    tasks: Mapped[list[Task]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    # NULL = unverified email. Verification is non-blocking (banner only).
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Bumped on logout / password change → invalidates outstanding
    # session cookies (which embed this value).
    session_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    memberships: Mapped[list[Membership]] = relationship(back_populates="user")
    oauth_identities: Mapped[list[OAuthIdentity]] = relationship(back_populates="user")

    @property
    def email_verified(self) -> bool:
        """Derived flag the API exposes (UserResponse reads it via from_attributes)."""
        return self.email_verified_at is not None


class OAuthIdentity(Base):
    __tablename__ = "oauth_identities"
    __table_args__ = (Index("ix_oauth_identities_user_id", "user_id"),)

    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    subject: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="oauth_identities")


class Membership(Base):
    __tablename__ = "memberships"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="memberships")
    company: Mapped[Company] = relationship(back_populates="members")


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    invited_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    accepted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    company: Mapped[Company] = relationship("Company", foreign_keys="[Invite.company_id]")
    inviter: Mapped[User] = relationship("User", foreign_keys="[Invite.invited_by]")


class Task(Base):
    """An authored (or seeded template) interview task.

    Replaces the filesystem `tasks/<id>/` layout: weakness + assessment spec live
    as parsed JSONB mappings (fed verbatim to the existing pure renderers/parsers),
    starter scaffold files as a JSONB `list[{path, content}]`. `slug` is
    unique per company. Reference tasks (task-01, task-02) are seeded as
    `is_template=True` rows owned by the system "Vibony Templates" company and
    surfaced read-only to every workspace.
    """

    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("company_id", "slug", name="uq_tasks_company_slug"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description_md: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=""
    )
    starter_files: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    runtime_image: Mapped[str] = mapped_column(Text, nullable=False)
    weakness: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    spec: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    visibility: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="workspace"
    )
    is_template: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    company: Mapped[Company] = relationship(back_populates="tasks")
    creator: Mapped[User | None] = relationship("User", foreign_keys="Task.created_by")


class TaskFavorite(Base):
    """A user's personal favorite task (dashboard task-picker UX).

    A ``(user, task)`` pair — the user's shortlist, surfaced first in the task
    picker and catalog. Personal to the user (no workspace/permission model): a
    favorite is the user's own data, so anyone may favorite any task they can see.
    Both FKs cascade, so deleting a user or a task drops their favorite rows.
    """

    __tablename__ = "task_favorites"
    __table_args__ = (
        UniqueConstraint("user_id", "task_id", name="uq_task_favorites_user_task"),
        Index("ix_task_favorites_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_company_created_desc", "company_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # Slug snapshot — kept for protocol / display / workdir-name stability.
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    # FK to the DB task: integrity link + cross-workspace resolver. NULL
    # for legacy rows until the seed backfills by slug, and when the task
    # is later deleted (ON DELETE SET NULL — the session degrades gracefully).
    task_ref_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    protocol_version: Mapped[int] = mapped_column(nullable=False)
    ai_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    bundle_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Nullable until auth is wired; FK ON DELETE CASCADE enforced at DB level.
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=True,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    candidate_token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    share_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Replay share-token expiry. Stamped at mint (now +
    # SHARE_TOKEN_TTL_DAYS); revoke nulls share_token + this. NULL = no token
    # minted (or a legacy token — treated as non-expiring for back-compat).
    share_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Interview mode. 'live' = interviewer watches, no timer;
    # 'async' = timer, no interviewer. time_limit_seconds is the async deadline
    # window (NULL for live); started_at anchors it (set on first candidate
    # connect).
    mode: Mapped[str] = mapped_column(Text, nullable=False, server_default="live")
    time_limit_seconds: Mapped[int | None] = mapped_column(nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Persisted live LLM message blob (AgentHost._messages), restored
    # verbatim on cold-start so tool-call rounds survive a backend restart.
    # NULL for legacy sessions (fall back to user/assistant event replay).
    agent_messages: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    # Private runtime checkpoint and usage ledger; never candidate-facing.
    agent_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    llm_usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    events: Mapped[list[Event]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="Event.seq",
    )
    company: Mapped[Company | None] = relationship(back_populates="sessions")
    creator: Mapped[User | None] = relationship(
        "User", foreign_keys="Session.created_by"
    )
    task_ref: Mapped[Task | None] = relationship(
        "Task", foreign_keys="Session.task_ref_id"
    )
    # 1:1 AI assessment artifact (overwritten on regenerate, no history).
    assessment: Mapped[SessionAssessment | None] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        uselist=False,
    )


class SessionAssessment(Base):
    """AI assessment artifact for a completed session.

    1:1 with Session (`session_id` UNIQUE), **overwritten** on regenerate — no
    history table. The event log stays canonical for the timeline/replay;
    this row is the canonical assessment artifact (mirrors the split: event
    log for the UI, a separate artifact for the derived product).

    `status` lifecycle: pending → running → ready | failed. `summary_md`/`findings`/
    `model` are populated only on `ready`; `error` only on `failed`.
    """

    __tablename__ = "session_assessments"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_session_assessments_session_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    findings: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    session: Mapped[Session] = relationship(back_populates="assessment")


class AuditLog(Base):
    """Append-only workspace activity log.

    One row per recorded action. Best-effort: a write failure must never break the
    action it records. `meta` maps to the DB column `metadata` (the bare name is
    reserved by SQLAlchemy's DeclarativeBase).
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_company_created_desc", "company_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    actor: Mapped[User | None] = relationship("User", foreign_keys="AuditLog.actor_user_id")


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("session_id", "id", name="uq_events_session_event_id"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    client_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    session: Mapped[Session] = relationship(back_populates="events")
