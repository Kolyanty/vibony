"""FastAPI dependency functions for auth + tenant access control."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy.orm import joinedload

from vibony_backend.auth.cookies import read_cookie
from vibony_backend.db.engine import session_scope
from vibony_backend.db.models import Company, Membership
from vibony_backend.db.models import Session as DbSession
from vibony_backend.db.models import User
from vibony_backend.schemas import MeResponse, MembershipResponse, UserResponse


async def build_me_response(user: User, db: AsyncSession) -> MeResponse:
    """Build MeResponse for a user — shared by /auth/me and invite signup."""
    rows = (
        await db.execute(
            select(Membership)
            .where(Membership.user_id == user.id)
            .options(joinedload(Membership.company))
        )
    ).scalars().unique().all()
    return MeResponse(
        user=UserResponse.model_validate(user),
        memberships=[
            MembershipResponse(
                workspace_id=m.company_id,
                company_name=m.company.name,
                role=m.role,
                plan=m.company.plan,
                trial_ends_at=m.company.trial_ends_at,
            )
            for m in rows
            if m.company is not None
        ],
    )


async def _user_from_cookie(request: Request, db: AsyncSession) -> User | None:
    """Resolve the cookie → User, enforcing the embedded session_version.
    A stale version (after logout / password change) resolves to None."""
    claims = read_cookie(request)
    if claims is None:
        return None
    user_id, sv = claims
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None or user.session_version != sv:
        return None
    return user


async def optional_current_user(
    request: Request,
    db: AsyncSession = Depends(session_scope),
) -> User | None:
    """Like current_user but returns None instead of raising 401."""
    return await _user_from_cookie(request, db)


async def current_user(
    request: Request,
    db: AsyncSession = Depends(session_scope),
) -> User:
    user = await _user_from_cookie(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail={"reason": "unauthorized"})
    return user


async def current_company(
    code: str,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(session_scope),
) -> tuple[DbSession, Company]:
    """Load session + verify caller is a member of that session's company."""
    row = (
        await db.execute(select(DbSession).where(DbSession.code == code))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"reason": "not_found", "code": code})

    if row.company_id is None:
        raise HTTPException(status_code=403, detail={"reason": "unauthorized"})

    membership = (
        await db.execute(
            select(Membership).where(
                Membership.user_id == user.id,
                Membership.company_id == row.company_id,
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=403, detail={"reason": "unauthorized"})

    company = (
        await db.execute(select(Company).where(Company.id == row.company_id))
    ).scalar_one()

    return row, company
