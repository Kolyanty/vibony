"""Workspace creation. Password-mode only — 404 in OIDC mode.

A logged-in user can spin up an additional workspace and becomes its admin. This
is the multi-workspace counterpart to the bootstrap company created at signup; the
switcher (dashboard) lets a user move between every workspace they belong to.

OIDC mode is single-installation (jit_provision in oidc.py), so multi-workspace
creation is disabled there — same posture as invites.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from vibony_backend.auth import audit
from vibony_backend.auth.deps import current_user
from vibony_backend.auth.invites import _require_company_admin
from vibony_backend.db.engine import session_scope
from vibony_backend.db.models import Company, Membership, User
from vibony_backend.schemas import MembershipResponse, WorkspaceCreateRequest

router = APIRouter(prefix="/workspaces", tags=["team"])

# Trial length for a freshly created workspace. Surfaced as the dashboard
# countdown badge; no billing enforcement.
TRIAL_PERIOD_DAYS = 14


def _require_password_mode() -> None:
    if os.getenv("VIBONY_AUTH_MODE", "password") != "password":
        raise HTTPException(status_code=404)


def trial_deadline(now: datetime | None = None) -> datetime:
    """The trial_ends_at anchor for a workspace created at ``now`` (default: utcnow)."""
    base = now or datetime.now(timezone.utc)
    return base + timedelta(days=TRIAL_PERIOD_DAYS)


@router.post("", response_model=MembershipResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: WorkspaceCreateRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(session_scope),
) -> MembershipResponse:
    _require_password_mode()

    # name is already stripped + non-blank by WorkspaceCreateRequest.
    company = Company(
        name=body.name,
        plan="trial",
        trial_ends_at=trial_deadline(),
    )
    db.add(company)
    await db.flush()
    db.add(Membership(user_id=user.id, company_id=company.id, role="admin"))
    await db.commit()
    await db.refresh(company)

    await audit.record(
        company_id=company.id,
        actor_user_id=user.id,
        action=audit.WORKSPACE_CREATED,
        target_type="workspace",
        target_id=str(company.id),
        metadata={"name": company.name},
    )

    return MembershipResponse(
        workspace_id=company.id,
        company_name=company.name,
        role="admin",
        plan=company.plan,
        trial_ends_at=company.trial_ends_at,
    )


@router.delete("/{company_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    company_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(session_scope),
) -> None:
    """Permanently delete a workspace and everything it owns. Admin-only.

    Destructive + irreversible: one DB DELETE cascades (ON DELETE CASCADE on every
    ``companies.id`` FK) to memberships, sessions (→ events, assessments), tasks
    (→ favorites), invites, and this workspace's audit trail. Deleting the last
    workspace a user belongs to is allowed — the dashboard then shows the
    zero-workspace onboarding screen.

    Unlike ``remove_member`` there is no last-admin / last-workspace guard: this
    tears the workspace down for every member at once. No audit row is written —
    it would be cascade-deleted with the workspace it records.

    Core DELETE (not ORM ``db.delete``) so the DB cascade does the work: the
    ``Company.sessions`` relationship has no ORM delete-cascade, so an ORM delete
    would instead NULL ``sessions.company_id`` and orphan them.
    """
    _require_password_mode()
    await _require_company_admin(company_id, user, db)
    await db.execute(delete(Company).where(Company.id == company_id))
    await db.commit()
