"""Workspace audit log.

`record(...)` appends one row in its **own** session (committed independently of the
caller's transaction) and swallows any failure — observability must never break the
action it records. Call it *after* the action has committed.

Action constants are the dotted strings stored in `audit_log.action`; the dashboard
maps them to localized labels.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import joinedload

from vibony_backend.db.engine import get_sessionmaker
from vibony_backend.db.models import AuditLog

logger = logging.getLogger(__name__)

# Action constants.
SESSION_CREATED = "session.created"
WORKSPACE_CREATED = "workspace.created"
MEMBER_INVITED = "member.invited"
MEMBER_JOINED = "member.joined"
MEMBER_ROLE_CHANGED = "member.role_changed"
MEMBER_REMOVED = "member.removed"
INVITE_REVOKED = "invite.revoked"


async def record(
    *,
    company_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Best-effort append to the audit log. Never raises into the caller."""
    try:
        sm = get_sessionmaker()
        async with sm() as db:
            db.add(
                AuditLog(
                    company_id=company_id,
                    actor_user_id=actor_user_id,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    meta=metadata or {},
                )
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — audit must not break the recorded action
        logger.warning("audit record failed (action=%s): %s", action, exc)


async def list_for_company(company_id: uuid.UUID, limit: int = 50) -> list[AuditLog]:
    """Most-recent-first audit rows for a workspace, with actor eager-loaded."""
    sm = get_sessionmaker()
    async with sm() as db:
        return list(
            (
                await db.execute(
                    select(AuditLog)
                    .where(AuditLog.company_id == company_id)
                    .options(joinedload(AuditLog.actor))
                    .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )


async def purge_for_target(
    company_id: uuid.UUID, target_type: str, target_id: str
) -> None:
    """Delete every audit row pointing at one target (erasure).

    Best-effort in its own session — used when a subject (e.g. a session) is erased
    so its audit trail is scrubbed too. Never raises into the caller."""
    try:
        sm = get_sessionmaker()
        async with sm() as db:
            await db.execute(
                delete(AuditLog).where(
                    AuditLog.company_id == company_id,
                    AuditLog.target_type == target_type,
                    AuditLog.target_id == target_id,
                )
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — scrubbing must not break the erasure
        logger.warning(
            "audit purge_for_target failed (%s=%s): %s", target_type, target_id, exc
        )


async def prune_older_than(days: int) -> int:
    """Hard-delete audit rows older than ``days`` (retention window).

    ``days <= 0`` keeps everything (the default). Best-effort in its own session;
    returns the number of rows removed (0 on failure)."""
    if days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        sm = get_sessionmaker()
        async with sm() as db:
            result = await db.execute(
                delete(AuditLog).where(AuditLog.created_at < cutoff)
            )
            await db.commit()
            return result.rowcount or 0
    except Exception as exc:  # noqa: BLE001 — retention prune must not break startup
        logger.warning("audit prune_older_than(%d) failed: %s", days, exc)
        return 0
