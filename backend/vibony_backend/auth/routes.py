from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vibony_backend.auth.cookies import clear_cookie, issue_cookie
from vibony_backend.auth.deps import build_me_response, current_user
from vibony_backend.auth.email import read_email_token, send_verification_email
from vibony_backend.auth.passwords import hash_password, verify_password
from vibony_backend.auth.workspaces import trial_deadline

from vibony_backend.db.engine import session_scope
from vibony_backend.db.models import Company, Membership, User
from vibony_backend.ratelimit import rate_limit, rate_limit_user
from vibony_backend.schemas import (
    AuthConfigResponse,
    CompanyResponse,
    LoginRequest,
    MeResponse,
    SignupRequest,
    SignupResponse,
    UserResponse,
    VerifyEmailRequest,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _require_password_mode() -> None:
    if os.getenv("VIBONY_AUTH_MODE", "password") != "password":
        raise HTTPException(status_code=404)


@router.post(
    "/signup",
    response_model=SignupResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("signup", 5, 60))],
)
async def signup(
    body: SignupRequest,
    response: Response,
    db: AsyncSession = Depends(session_scope),
) -> SignupResponse:
    _require_password_mode()

    existing = (
        await db.execute(select(User).where(User.email == body.email))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail={"reason": "duplicate_email"})

    user = User(email=body.email, password_hash=hash_password(body.password))
    company = Company(name=body.company_name, plan="trial", trial_ends_at=trial_deadline())
    db.add(user)
    db.add(company)
    # The pre-check above is advisory only — two concurrent signups for
    # the same email both pass it, then collide on the users.email unique index at
    # INSERT. Catch it as a clean 409 (mirrors signup_and_accept_invite) instead of
    # a 500. The membership + commit are inside the guard so the constraint hit on
    # either flush or commit is funneled to the same response.
    try:
        await db.flush()
        db.add(Membership(user_id=user.id, company_id=company.id, role="admin"))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail={"reason": "duplicate_email"})
    await db.refresh(user)
    await db.refresh(company)

    send_verification_email(user.email, user.id)
    issue_cookie(user.id, user.session_version, response)
    return SignupResponse(
        user=UserResponse.model_validate(user),
        company=CompanyResponse.model_validate(company),
    )


@router.post(
    "/login",
    response_model=MeResponse,
    dependencies=[Depends(rate_limit("login", 10, 60))],
)
async def login(
    body: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(session_scope),
) -> MeResponse:
    _require_password_mode()

    user = (
        await db.execute(select(User).where(User.email == body.email))
    ).scalar_one_or_none()

    # Identical error for unknown email and wrong password — no enumeration.
    if (
        user is None
        or user.password_hash is None
        or not verify_password(body.password, user.password_hash)
    ):
        raise HTTPException(status_code=401, detail={"reason": "invalid_credentials"})

    issue_cookie(user.id, user.session_version, response)
    return await build_me_response(user, db)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(session_scope),
) -> None:
    # Bump session_version → every outstanding cookie for this user is invalidated
    # server-side, not just the one in this browser.
    user.session_version += 1
    await db.commit()
    clear_cookie(response)


@router.get("/me", response_model=MeResponse)
async def me(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(session_scope),
) -> MeResponse:
    return await build_me_response(user, db)


@router.get("/config", response_model=AuthConfigResponse)
async def auth_config() -> AuthConfigResponse:
    """Return auth mode metadata — dashboard uses this to render the correct login page."""
    mode = os.getenv("VIBONY_AUTH_MODE", "password")
    return AuthConfigResponse(
        mode=mode,  # type: ignore[arg-type]
        oidc_login_url="/auth/oidc/start" if mode == "oidc" else None,
    )


@router.post("/verify-email", status_code=status.HTTP_200_OK)
async def verify_email(
    body: VerifyEmailRequest,
    db: AsyncSession = Depends(session_scope),
) -> dict:
    """Confirm an email from a verification token. No auth — the token is the proof.

    Idempotent: re-confirming an already-verified user is a no-op 200. An invalid or
    expired token → 400 (the user resends from the dashboard banner).
    """
    user_id = read_email_token(body.token)
    if user_id is None:
        raise HTTPException(status_code=400, detail={"reason": "invalid_token"})
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=400, detail={"reason": "invalid_token"})
    if user.email_verified_at is None:
        user.email_verified_at = datetime.now(timezone.utc)
        await db.commit()
    return {"status": "ok"}


@router.post(
    "/verify-email/resend",
    status_code=status.HTTP_202_ACCEPTED,
    # Keyed per-user, not per-IP — users behind a shared NAT must not
    # share (and exhaust) one resend bucket.
    dependencies=[Depends(rate_limit_user("resend_verify", 3, 60))],
)
async def resend_verification(
    user: User = Depends(current_user),
) -> dict:
    """Re-send the verification email to the logged-in user (no-op if already verified)."""
    if user.email_verified_at is None:
        send_verification_email(user.email, user.id)
    return {"status": "ok"}
