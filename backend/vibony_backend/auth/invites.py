"""Invite flow endpoints. Password-mode only — all routes 404 in OIDC mode."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from vibony_backend.auth import audit
from vibony_backend.auth.cookies import issue_cookie
from vibony_backend.auth.deps import build_me_response, current_user
from vibony_backend.auth.email import send_verification_email
from vibony_backend.auth.passwords import hash_password
from vibony_backend.db.engine import session_scope
from vibony_backend.db.models import Company, Invite, Membership, User
from vibony_backend.schemas import (
    AuditLogItem,
    AuditLogResponse,
    InviteCreateRequest,
    InvitePreview,
    InviteResponse,
    InviteSignupRequest,
    InviteStatus,
    MeResponse,
    MemberListItem,
    MemberListResponse,
    MemberRoleUpdateRequest,
)

router = APIRouter(tags=["team"])

_REVOKE_SENTINEL = datetime(1970, 1, 1, tzinfo=timezone.utc)
_INVITE_TTL_DAYS = 7


def _require_password_mode() -> None:
    if os.getenv("VIBONY_AUTH_MODE", "password") != "password":
        raise HTTPException(status_code=404)


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def _to_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _invite_status(invite: Invite) -> InviteStatus:
    if invite.accepted_at is not None:
        return "accepted"
    expires = _to_utc(invite.expires_at)
    created = _to_utc(invite.created_at)
    if expires < created:
        return "revoked"
    if expires < datetime.now(timezone.utc):
        return "expired"
    return "pending"


def _to_invite_response(invite: Invite, accept_url: str | None = None) -> InviteResponse:
    return InviteResponse(
        id=invite.id,
        workspace_id=invite.company_id,
        token=invite.token,
        email=invite.email,
        role=invite.role,
        invited_by=invite.invited_by,
        created_at=invite.created_at,
        expires_at=invite.expires_at,
        accepted_at=invite.accepted_at,
        accepted_by=invite.accepted_by,
        status=_invite_status(invite),
        accept_url=accept_url,
    )


async def _require_company_admin(
    company_id: uuid.UUID,
    user: User,
    db: AsyncSession,
) -> Company:
    company = (
        await db.execute(select(Company).where(Company.id == company_id))
    ).scalar_one_or_none()
    if company is None:
        raise HTTPException(status_code=404, detail={"reason": "not_found"})
    m = (
        await db.execute(
            select(Membership).where(
                Membership.user_id == user.id,
                Membership.company_id == company_id,
            )
        )
    ).scalar_one_or_none()
    if m is None or m.role != "admin":
        raise HTTPException(status_code=403, detail={"reason": "forbidden"})
    return company


# -------- GET /workspaces/{company_id}/members --------


@router.get("/workspaces/{company_id}/members", response_model=MemberListResponse)
async def list_members(
    company_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(session_scope),
) -> MemberListResponse:
    _require_password_mode()
    await _require_company_admin(company_id, user, db)
    rows = (
        await db.execute(
            select(Membership)
            .where(Membership.company_id == company_id)
            .options(joinedload(Membership.user))
            .order_by(Membership.created_at)
        )
    ).scalars().unique().all()
    return MemberListResponse(
        members=[
            MemberListItem(
                user_id=m.user_id,
                email=m.user.email,
                role=m.role,
                joined_at=m.created_at,
            )
            for m in rows
        ]
    )


async def _count_admins(company_id: uuid.UUID, db: AsyncSession) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(Membership)
            .where(Membership.company_id == company_id, Membership.role == "admin")
        )
    ).scalar_one()


async def _lock_company(company_id: uuid.UUID, db: AsyncSession) -> None:
    """Serialize member mutations on a workspace via a row lock on the company.

    The last-admin guard is count-then-mutate; under READ COMMITTED two concurrent
    admin demotions/removals could each read count==2 and both commit, orphaning the
    workspace with zero admins. Taking `SELECT companies … FOR UPDATE` before the
    count makes the second transaction block until the first commits, then re-count.
    """
    await db.execute(select(Company.id).where(Company.id == company_id).with_for_update())


async def _inviter_still_admin(invite: Invite, db: AsyncSession) -> bool:
    """An invite is the inviter's authority, exercised later at accept time.

    If the inviter has since been demoted or removed from the workspace, the
    snapshotted role must NOT still be granted — re-check the inviter holds admin
    in `invite.company_id` before honoring the invite.
    """
    m = (
        await db.execute(
            select(Membership).where(
                Membership.user_id == invite.invited_by,
                Membership.company_id == invite.company_id,
            )
        )
    ).scalar_one_or_none()
    return m is not None and m.role == "admin"


async def _load_membership(
    company_id: uuid.UUID, user_id: uuid.UUID, db: AsyncSession
) -> Membership:
    m = (
        await db.execute(
            select(Membership)
            .where(Membership.company_id == company_id, Membership.user_id == user_id)
            .options(joinedload(Membership.user))
        )
    ).scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=404, detail={"reason": "not_found"})
    return m


# -------- GET /workspaces/{company_id}/audit --------


@router.get("/workspaces/{company_id}/audit", response_model=AuditLogResponse)
async def list_audit(
    company_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(session_scope),
) -> AuditLogResponse:
    """Most-recent workspace activity. Admin-only."""
    _require_password_mode()
    await _require_company_admin(company_id, user, db)
    rows = await audit.list_for_company(company_id)
    target_emails = await _resolve_target_emails(rows, db)
    return AuditLogResponse(
        entries=[
            AuditLogItem(
                id=r.id,
                action=r.action,
                actor_email=r.actor.email if r.actor is not None else None,
                target_type=r.target_type,
                target_id=r.target_id,
                target_email=target_emails.get((r.target_type, r.target_id)),
                metadata=r.meta,
                created_at=r.created_at,
            )
            for r in rows
        ]
    )


def _maybe_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError):
        return None


async def _resolve_target_emails(
    rows: list, db: AsyncSession
) -> dict[tuple[str | None, str | None], str | None]:
    """Resolve each audit row's target email at read time instead of
    storing it in metadata — so a user/invite that is later erased drops out of
    the audit view automatically. Targets a ``user`` (→ ``users.email``) or an
    ``invite`` (→ the invited address). Batched to avoid N+1."""
    user_ids: set[uuid.UUID] = set()
    invite_ids: set[uuid.UUID] = set()
    for r in rows:
        parsed = _maybe_uuid(r.target_id)
        if parsed is None:
            continue
        if r.target_type == "user":
            user_ids.add(parsed)
        elif r.target_type == "invite":
            invite_ids.add(parsed)

    by_user: dict[uuid.UUID, str] = {}
    if user_ids:
        for uid, email in (
            await db.execute(select(User.id, User.email).where(User.id.in_(user_ids)))
        ).all():
            by_user[uid] = email
    by_invite: dict[uuid.UUID, str | None] = {}
    if invite_ids:
        for iid, email in (
            await db.execute(
                select(Invite.id, Invite.email).where(Invite.id.in_(invite_ids))
            )
        ).all():
            by_invite[iid] = email

    out: dict[tuple[str | None, str | None], str | None] = {}
    for r in rows:
        parsed = _maybe_uuid(r.target_id)
        if parsed is None:
            continue
        if r.target_type == "user":
            out[(r.target_type, r.target_id)] = by_user.get(parsed)
        elif r.target_type == "invite":
            out[(r.target_type, r.target_id)] = by_invite.get(parsed)
    return out


# -------- PATCH /workspaces/{company_id}/members/{user_id} --------


@router.patch(
    "/workspaces/{company_id}/members/{user_id}", response_model=MemberListItem
)
async def update_member_role(
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    body: MemberRoleUpdateRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(session_scope),
) -> MemberListItem:
    """Change a member's role. Admin-only; never demote the last admin (409)."""
    _require_password_mode()
    await _require_company_admin(company_id, user, db)
    await _lock_company(company_id, db)
    membership = await _load_membership(company_id, user_id, db)

    if membership.role != body.role:
        # Block demoting the workspace's only admin — it would orphan the workspace.
        if membership.role == "admin" and await _count_admins(company_id, db) <= 1:
            raise HTTPException(status_code=409, detail={"reason": "last_admin"})
        previous = membership.role
        membership.role = body.role
        await db.commit()
        # Don't freeze the target's email into audit metadata — it is
        # resolved at read time from target_id (the user) so erasure scrubs it.
        await audit.record(
            company_id=company_id,
            actor_user_id=user.id,
            action=audit.MEMBER_ROLE_CHANGED,
            target_type="user",
            target_id=str(membership.user_id),
            metadata={"from": previous, "to": body.role},
        )

    return MemberListItem(
        user_id=membership.user_id,
        email=membership.user.email,
        role=membership.role,
        joined_at=membership.created_at,
    )


# -------- DELETE /workspaces/{company_id}/members/{user_id} --------


@router.delete(
    "/workspaces/{company_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_member(
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(session_scope),
) -> None:
    """Remove a member from the workspace. Admin-only; never remove the last admin (409)."""
    _require_password_mode()
    await _require_company_admin(company_id, user, db)
    await _lock_company(company_id, db)
    membership = await _load_membership(company_id, user_id, db)

    if membership.role == "admin" and await _count_admins(company_id, db) <= 1:
        raise HTTPException(status_code=409, detail={"reason": "last_admin"})

    await db.delete(membership)
    await db.commit()
    # Email resolved at read time from target_id (the user row survives a
    # membership removal), not frozen into metadata.
    await audit.record(
        company_id=company_id,
        actor_user_id=user.id,
        action=audit.MEMBER_REMOVED,
        target_type="user",
        target_id=str(user_id),
        metadata={},
    )


# -------- POST /workspaces/{company_id}/invites --------


@router.post(
    "/workspaces/{company_id}/invites",
    response_model=InviteResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_invite(
    company_id: uuid.UUID,
    body: InviteCreateRequest,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(session_scope),
) -> InviteResponse:
    _require_password_mode()
    await _require_company_admin(company_id, user, db)
    # An admin grant must be bound to a specific email — otherwise the invite
    # link is a self-service admin role for any holder (incl. an account they
    # control). An email-bound invite is also enforced at accept (email_mismatch).
    if body.role == "admin" and body.email is None:
        raise HTTPException(
            status_code=422, detail={"reason": "admin_invite_requires_email"}
        )
    token = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    invite = Invite(
        company_id=company_id,
        token=token,
        email=body.email,
        role=body.role,
        invited_by=user.id,
        created_at=now,
        expires_at=now + timedelta(days=_INVITE_TTL_DAYS),
    )
    db.add(invite)
    await db.commit()
    await db.refresh(invite)
    await audit.record(
        company_id=company_id,
        actor_user_id=user.id,
        action=audit.MEMBER_INVITED,
        target_type="invite",
        target_id=str(invite.id),
        # Invited email resolved at read time from the invite row.
        metadata={"role": body.role},
    )
    frontend_url = os.getenv("VIBONY_FRONTEND_URL", "http://localhost:5173").rstrip("/")
    accept_url = f"{frontend_url}/invite/{token}"
    return _to_invite_response(invite, accept_url=accept_url)


# -------- GET /workspaces/{company_id}/invites --------


@router.get("/workspaces/{company_id}/invites", response_model=list[InviteResponse])
async def list_invites(
    company_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(session_scope),
) -> list[InviteResponse]:
    _require_password_mode()
    await _require_company_admin(company_id, user, db)
    invites = (
        await db.execute(
            select(Invite)
            .where(Invite.company_id == company_id)
            .order_by(Invite.created_at.desc())
        )
    ).scalars().all()
    return [_to_invite_response(inv) for inv in invites]


# -------- DELETE /invites/{token} --------


@router.delete("/invites/{token}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(
    token: str,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(session_scope),
) -> None:
    _require_password_mode()
    invite = (
        await db.execute(select(Invite).where(Invite.token == token))
    ).scalar_one_or_none()
    if invite is None:
        raise HTTPException(status_code=404, detail={"reason": "not_found"})
    await _require_company_admin(invite.company_id, user, db)
    invite.expires_at = _REVOKE_SENTINEL
    await db.commit()
    await audit.record(
        company_id=invite.company_id,
        actor_user_id=user.id,
        action=audit.INVITE_REVOKED,
        target_type="invite",
        target_id=str(invite.id),
        # Invited email resolved at read time from the invite row.
        metadata={},
    )


# -------- GET /invites/{token} --------


@router.get("/invites/{token}", response_model=InvitePreview)
async def preview_invite(
    token: str,
    db: AsyncSession = Depends(session_scope),
) -> InvitePreview:
    _require_password_mode()
    invite = (
        await db.execute(
            select(Invite)
            .where(Invite.token == token)
            .options(
                joinedload(Invite.company),
                joinedload(Invite.inviter),
            )
        )
    ).scalar_one_or_none()
    if invite is None:
        raise HTTPException(status_code=404, detail={"reason": "not_found"})
    return InvitePreview(
        company_name=invite.company.name,
        inviter_email=_mask_email(invite.inviter.email),
        invited_email=invite.email,
        role=invite.role,
        expires_at=invite.expires_at,
        status=_invite_status(invite),
    )


# -------- POST /invites/{token}/accept --------


@router.post("/invites/{token}/accept", status_code=status.HTTP_200_OK)
async def accept_invite(
    token: str,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(session_scope),
) -> dict:
    _require_password_mode()
    invite = (
        await db.execute(select(Invite).where(Invite.token == token))
    ).scalar_one_or_none()
    if invite is None:
        raise HTTPException(status_code=404, detail={"reason": "not_found"})
    st = _invite_status(invite)
    if st != "pending":
        raise HTTPException(status_code=410, detail={"reason": st})
    # Re-validate the inviter's authority at accept (not just at create).
    if not await _inviter_still_admin(invite, db):
        raise HTTPException(status_code=410, detail={"reason": "inviter_unauthorized"})
    # If invite was created for a specific email, enforce it.
    if invite.email is not None and invite.email.lower() != user.email.lower():
        raise HTTPException(status_code=403, detail={"reason": "email_mismatch"})
    existing = (
        await db.execute(
            select(Membership).where(
                Membership.user_id == user.id,
                Membership.company_id == invite.company_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(Membership(
            user_id=user.id,
            company_id=invite.company_id,
            role=invite.role,
        ))
    invite.accepted_at = datetime.now(timezone.utc)
    invite.accepted_by = user.id
    await db.commit()
    await audit.record(
        company_id=invite.company_id,
        actor_user_id=user.id,
        action=audit.MEMBER_JOINED,
        target_type="user",
        target_id=str(user.id),
        # Email resolved at read time from target_id (the user).
        metadata={"role": invite.role},
    )
    return {"status": "ok"}


# -------- POST /invites/{token}/signup-and-accept --------


@router.post("/invites/{token}/signup-and-accept", response_model=MeResponse)
async def signup_and_accept_invite(
    token: str,
    body: InviteSignupRequest,
    response: Response,
    db: AsyncSession = Depends(session_scope),
) -> MeResponse:
    """Create a new user (no company) and immediately accept the invite.

    Used by AcceptInvitePage when the visitor is not logged in.
    Regular signup creates a spurious company — this endpoint avoids that.
    """
    _require_password_mode()

    invite = (
        await db.execute(
            select(Invite).where(Invite.token == token)
        )
    ).scalar_one_or_none()
    if invite is None:
        raise HTTPException(status_code=404, detail={"reason": "not_found"})

    st = _invite_status(invite)
    if st != "pending":
        raise HTTPException(status_code=410, detail={"reason": st})

    # Re-validate the inviter's authority at accept (not just at create).
    if not await _inviter_still_admin(invite, db):
        raise HTTPException(status_code=410, detail={"reason": "inviter_unauthorized"})

    if invite.email is not None and invite.email.lower() != body.email.lower():
        raise HTTPException(status_code=403, detail={"reason": "email_mismatch"})

    existing_user = (
        await db.execute(select(User).where(User.email == body.email))
    ).scalar_one_or_none()
    if existing_user is not None:
        raise HTTPException(status_code=409, detail={"reason": "duplicate_email"})

    user = User(email=body.email, password_hash=hash_password(body.password))
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail={"reason": "duplicate_email"})

    db.add(Membership(user_id=user.id, company_id=invite.company_id, role=invite.role))
    invite.accepted_at = datetime.now(timezone.utc)
    invite.accepted_by = user.id
    await db.commit()
    await db.refresh(user)

    await audit.record(
        company_id=invite.company_id,
        actor_user_id=user.id,
        action=audit.MEMBER_JOINED,
        target_type="user",
        target_id=str(user.id),
        # Email resolved at read time from target_id (the user).
        metadata={"role": invite.role},
    )
    send_verification_email(user.email, user.id)
    issue_cookie(user.id, user.session_version, response)
    return await build_me_response(user, db)
