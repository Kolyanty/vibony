"""Backend auth core (password mode) tests.

Uses the shared http_client fixture (per-function: truncates tables each test)
for HTTP-level tests; calls dep functions directly for unit-level tests.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vibony_backend.auth.deps import current_company
from vibony_backend.db.models import Company, Membership
from vibony_backend.db.models import Session as DbSession
from vibony_backend.db.models import User

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Module-level env setup — applied to every test in this file.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBONY_AUTH_MODE", "password")
    monkeypatch.setenv("VIBONY_SESSION_SECRET", "test-secret-" + "x" * 40)
    monkeypatch.setenv("VIBONY_COOKIE_SECURE", "false")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _signup(client: AsyncClient, email: str = "user@example.com") -> dict:
    r = await client.post(
        "/auth/signup",
        json={"email": email, "password": "password123", "company_name": "Acme"},
    )
    assert r.status_code == 201, r.text
    return r


async def _login(client: AsyncClient, email: str = "user@example.com") -> dict:
    r = await client.post(
        "/auth/login",
        json={"email": email, "password": "password123"},
    )
    assert r.status_code == 200, r.text
    return r


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------

async def test_signup_happy(http_client: AsyncClient) -> None:
    r = await http_client.post(
        "/auth/signup",
        json={"email": "new@example.com", "password": "password123", "company_name": "Acme"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["user"]["email"] == "new@example.com"
    assert body["company"]["name"] == "Acme"
    assert "vibony_session" in r.cookies


async def test_signup_duplicate_email(http_client: AsyncClient) -> None:
    await _signup(http_client, "dup@example.com")
    r = await http_client.post(
        "/auth/signup",
        json={"email": "dup@example.com", "password": "password123", "company_name": "Other"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "duplicate_email"


async def test_signup_weak_password(http_client: AsyncClient) -> None:
    r = await http_client.post(
        "/auth/signup",
        json={"email": "weak@example.com", "password": "short", "company_name": "X"},
    )
    assert r.status_code == 422


async def test_signup_blank_company_name_rejected(http_client: AsyncClient) -> None:
    """A whitespace-only company name passes min_length but is rejected."""
    r = await http_client.post(
        "/auth/signup",
        json={"email": "blank@example.com", "password": "password123", "company_name": "   "},
    )
    assert r.status_code == 422


async def test_signup_strips_company_name(http_client: AsyncClient) -> None:
    """Surrounding whitespace is stripped before storage."""
    r = await http_client.post(
        "/auth/signup",
        json={"email": "padded@example.com", "password": "password123", "company_name": "  Acme  "},
    )
    assert r.status_code == 201, r.text
    assert r.json()["company"]["name"] == "Acme"


async def test_signup_duplicate_email_race_is_409_not_500() -> None:
    """The email pre-check is advisory; a concurrent insert that slips
    past it collides on the unique index. The handler must funnel that
    IntegrityError to a 409, not a 500.

    Driven at the unit level with a fake session whose pre-check reports "no such
    user" but whose flush raises IntegrityError — the exact race window."""
    from fastapi import HTTPException, Response
    from sqlalchemy.exc import IntegrityError

    from vibony_backend.auth.routes import signup
    from vibony_backend.schemas import SignupRequest

    class _Result:
        def scalar_one_or_none(self):
            return None  # pre-check sees no existing user

    class _FakeDB:
        def __init__(self) -> None:
            self.rolled_back = False

        async def execute(self, *a, **k):
            return _Result()

        def add(self, _obj) -> None:
            pass

        async def flush(self) -> None:
            raise IntegrityError("INSERT", {}, Exception("duplicate key"))

        async def commit(self) -> None:  # pragma: no cover - not reached
            pass

        async def rollback(self) -> None:
            self.rolled_back = True

        async def refresh(self, _obj) -> None:  # pragma: no cover - not reached
            pass

    fake = _FakeDB()
    body = SignupRequest(email="race@example.com", password="password123", company_name="Acme")
    with pytest.raises(HTTPException) as exc:
        await signup(body=body, response=Response(), db=fake)  # type: ignore[arg-type]
    assert exc.value.status_code == 409
    assert exc.value.detail["reason"] == "duplicate_email"
    assert fake.rolled_back


async def test_signup_concurrent_same_email_one_winner(http_client: AsyncClient) -> None:
    """Integration: fire several concurrent signups for one email;
    exactly one succeeds, the rest are 409, and none is a 500."""
    import asyncio

    payload = {"email": "concurrent@example.com", "password": "password123", "company_name": "Co"}
    results = await asyncio.gather(
        *(http_client.post("/auth/signup", json=payload) for _ in range(5))
    )
    statuses = [r.status_code for r in results]
    assert statuses.count(201) == 1, statuses
    assert all(s in (201, 409) for s in statuses), statuses
    assert 500 not in statuses, statuses


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

async def test_login_happy(http_client: AsyncClient) -> None:
    await _signup(http_client, "login@example.com")
    r = await http_client.post(
        "/auth/login",
        json={"email": "login@example.com", "password": "password123"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["email"] == "login@example.com"
    assert len(body["memberships"]) == 1
    assert body["memberships"][0]["role"] == "admin"
    assert "vibony_session" in r.cookies


async def test_login_wrong_password(http_client: AsyncClient) -> None:
    await _signup(http_client, "wrongpw@example.com")
    r = await http_client.post(
        "/auth/login",
        json={"email": "wrongpw@example.com", "password": "badpassword"},
    )
    assert r.status_code == 401
    assert r.json()["detail"]["reason"] == "invalid_credentials"


async def test_login_unknown_email(http_client: AsyncClient) -> None:
    r = await http_client.post(
        "/auth/login",
        json={"email": "nobody@example.com", "password": "password123"},
    )
    assert r.status_code == 401
    # Same shape as wrong password — no enumeration
    assert r.json()["detail"]["reason"] == "invalid_credentials"


# ---------------------------------------------------------------------------
# /auth/me
# ---------------------------------------------------------------------------

async def test_me_with_cookie(http_client: AsyncClient) -> None:
    signup_r = await _signup(http_client, "me@example.com")
    # Re-use the cookie from signup
    r = await http_client.get("/auth/me", cookies=signup_r.cookies)
    assert r.status_code == 200
    assert r.json()["user"]["email"] == "me@example.com"


async def test_me_without_cookie(http_client: AsyncClient) -> None:
    r = await http_client.get("/auth/me")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# /auth/logout
# ---------------------------------------------------------------------------

async def test_logout_clears_cookie(http_client: AsyncClient) -> None:
    signup_r = await _signup(http_client, "logout@example.com")
    r = await http_client.post("/auth/logout", cookies=signup_r.cookies)
    assert r.status_code == 204
    # After logout, cookie should be cleared (set-cookie with empty value / max-age=0)
    assert "vibony_session" in r.headers.get("set-cookie", "")


# ---------------------------------------------------------------------------
# current_company — unit test (cross-tenant access)
# ---------------------------------------------------------------------------

async def test_current_company_cross_tenant_denied(db_session: AsyncSession) -> None:
    """User from company_b cannot access a session belonging to company_a."""
    import datetime as dt
    from fastapi import HTTPException

    company_a = Company(name="CompanyA")
    company_b = Company(name="CompanyB")
    user_a = User(email="usera@test.com", password_hash="x")
    user_b = User(email="userb@test.com", password_hash="x")
    for obj in (company_a, company_b, user_a, user_b):
        db_session.add(obj)
    await db_session.flush()

    db_session.add(Membership(user_id=user_a.id, company_id=company_a.id, role="admin"))
    db_session.add(Membership(user_id=user_b.id, company_id=company_b.id, role="admin"))

    session_a = DbSession(
        code="CROSS-TENANT",
        status="pending",
        task_id="task-02-seat-reservation",
        protocol_version=2,
        ai_config={"primary_model": "x", "fallback_models": []},
        candidate_token="tok-cross-tenant",
        expires_at=dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc),
        company_id=company_a.id,
    )
    db_session.add(session_a)
    await db_session.flush()

    with pytest.raises(HTTPException) as exc:
        await current_company(code="CROSS-TENANT", user=user_b, db=db_session)
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# OIDC mode: password-mode-only routes return 404
# ---------------------------------------------------------------------------

async def test_oidc_mode_signup_returns_404(
    http_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBONY_AUTH_MODE", "oidc")
    r = await http_client.post(
        "/auth/signup",
        json={"email": "x@x.com", "password": "password123", "company_name": "X"},
    )
    assert r.status_code == 404


async def test_oidc_mode_login_returns_404(
    http_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBONY_AUTH_MODE", "oidc")
    r = await http_client.post(
        "/auth/login",
        json={"email": "x@x.com", "password": "password123"},
    )
    assert r.status_code == 404
