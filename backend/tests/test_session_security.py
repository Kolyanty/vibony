"""Session secret validation, cookie revocation, session TTL."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import update

from vibony_backend.db.engine import get_sessionmaker
from vibony_backend.db.models import Session as DbSession

_session_loop = pytest.mark.asyncio(loop_scope="session")

VALID_PAYLOAD = {
    "task_id": "task-01-training-loop",
    "ai_config": {"primary_model": "deepseek/deepseek-chat"},
}


# ---------------------------------------------------------------------------
# Secret strength validation
# ---------------------------------------------------------------------------

def test_validate_session_secret_rejects_short(monkeypatch):
    from vibony_backend.auth import cookies

    monkeypatch.setenv("VIBONY_SESSION_SECRET", "short")
    with pytest.raises(RuntimeError, match="at least"):
        cookies.validate_session_secret()


def test_validate_session_secret_accepts_strong(monkeypatch):
    from vibony_backend.auth import cookies

    monkeypatch.setenv("VIBONY_SESSION_SECRET", "x" * 32)
    cookies.validate_session_secret()  # must not raise


# ---------------------------------------------------------------------------
# Logout bumps session_version → old cookie is rejected everywhere
# ---------------------------------------------------------------------------

@_session_loop
async def test_logout_invalidates_outstanding_cookie(authed_client):
    client, _ = authed_client
    # The signup cookie is in the jar; capture it before logout clears it.
    old_cookie = client.cookies.get("vibony_session")
    assert old_cookie

    assert (await client.get("/auth/me")).status_code == 200

    assert (await client.post("/auth/logout")).status_code == 204

    # Replaying the pre-logout cookie must now fail (session_version bumped).
    resp = await client.get("/auth/me", cookies={"vibony_session": old_cookie})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# An expired session's candidate_token stops working
# ---------------------------------------------------------------------------

@_session_loop
async def test_expired_session_candidate_token_410(authed_client):
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code, token = created["session_code"], created["candidate_token"]

    # Fresh session: candidate bootstrap works.
    ok = await client.get(f"/sessions/{code}", params={"candidate_token": token})
    assert ok.status_code == 200

    # Force the deadline into the past.
    sm = get_sessionmaker()
    async with sm() as db:
        await db.execute(
            update(DbSession)
            .where(DbSession.code == code)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
        )
        await db.commit()

    expired = await client.get(f"/sessions/{code}", params={"candidate_token": token})
    assert expired.status_code == 410
    assert expired.json()["detail"]["reason"] == "completed"
