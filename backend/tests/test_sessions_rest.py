"""REST endpoints for sessions.

Changes:
- POST /sessions requires cookie auth (use authed_client fixture).
- GET /sessions (new) returns tenant-scoped list.
- GET /sessions/{code} requires ?candidate_token=.
- POST /sessions/{code}/end requires cookie auth (not Bearer).
- GET /sessions/{code}/events requires cookie auth.
- GET /sessions/{code}/bundle.tar.gz requires ?candidate_token=.
- POST /sessions/{code}/share requires cookie auth.
- Files endpoints require cookie auth.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vibony_backend.db.models import Event, Session as DbSession, Task, User
from vibony_backend.sessions import PROTOCOL_VERSION
from vibony_backend.sessions.codes import (
    SESSION_CODE_ALPHABET,
    generate_session_code,
)
from vibony_backend.tasks.images import DEFAULT_TEMPLATE_IMAGE

CODE_PATTERN = re.compile(rf"^[{SESSION_CODE_ALPHABET}]{{4}}-[{SESSION_CODE_ALPHABET}]{{4}}$")

VALID_PAYLOAD = {
    "task_id": "task-01-training-loop",
    "ai_config": {
        "primary_model": "deepseek/deepseek-chat",
        "fallback_models": ["anthropic/claude-haiku-4-5"],
    },
}


def test_generate_session_code_format():
    for _ in range(100):
        code = generate_session_code()
        assert CODE_PATTERN.match(code), f"bad code: {code}"


@pytest.mark.asyncio(loop_scope="session")
async def test_create_session_success(authed_client):
    client, _ = authed_client
    response = await client.post("/sessions", json=VALID_PAYLOAD)
    assert response.status_code == 201, response.text
    body = response.json()

    assert CODE_PATTERN.match(body["session_code"]), body["session_code"]
    assert body["session_status"] == "pending"
    assert body["protocol_version"] == PROTOCOL_VERSION
    assert body["task_id"] == "task-01-training-loop"
    assert body["task_dir_name"] == "training-loop"
    assert body["ai_config"]["primary_model"] == "deepseek/deepseek-chat"
    assert body["mode"] == "live"
    assert body["time_limit_seconds"] is None
    assert body["bundle_sha256"] is None
    assert body["candidate_token"]
    assert body["candidate_token"] != body["session_code"]
    # ws_token is empty for the create response (interviewer side); the candidate uses candidate_token directly
    assert body["ws_token"] == ""
    created = datetime.fromisoformat(body["created_at"])
    expires = datetime.fromisoformat(body["expires_at"])
    assert (expires - created).total_seconds() == pytest.approx(8 * 3600, abs=2)


@pytest.mark.asyncio(loop_scope="session")
async def test_create_session_requires_auth(http_client: AsyncClient):
    response = await http_client.post("/sessions", json=VALID_PAYLOAD)
    assert response.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
async def test_create_session_unknown_task(authed_client):
    client, _ = authed_client
    payload = dict(VALID_PAYLOAD, task_id="task-does-not-exist")
    response = await client.post("/sessions", json=payload)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["reason"] == "unknown_task"
    assert detail["task_id"] == "task-does-not-exist"


@pytest.mark.asyncio(loop_scope="session")
async def test_create_session_rejects_non_allowlisted_model(authed_client):
    """primary_model outside the allowlist / with an api_base → 422."""
    client, _ = authed_client
    bad = dict(VALID_PAYLOAD, ai_config={"primary_model": "openai/gpt-4@http://169.254.169.254"})
    resp = await client.post("/sessions", json=bad)
    assert resp.status_code == 422, resp.text

    bad2 = dict(VALID_PAYLOAD, ai_config={"primary_model": "anthropic/claude-not-allowed"})
    resp2 = await client.post("/sessions", json=bad2)
    assert resp2.status_code == 422, resp2.text


@pytest.mark.asyncio(loop_scope="session")
async def test_create_session_wrong_company(authed_client):
    """workspace_id that doesn't match the user's memberships → 403."""
    client, _ = authed_client
    payload = dict(VALID_PAYLOAD, workspace_id=str(uuid.uuid4()))
    response = await client.post("/sessions", json=payload)
    assert response.status_code == 403


@pytest.mark.asyncio(loop_scope="session")
async def test_create_session_defaults_from_env(authed_client, monkeypatch):
    client, _ = authed_client
    monkeypatch.setenv("VIBONY_DEFAULT_MODEL", "deepseek/deepseek-chat")
    response = await client.post("/sessions", json={"task_id": "task-01-training-loop"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["ai_config"]["primary_model"] == "deepseek/deepseek-chat"
    assert body["ai_config"]["fallback_models"] == []


@pytest.mark.asyncio(loop_scope="session")
async def test_create_session_no_model_no_env(authed_client, monkeypatch):
    client, _ = authed_client
    monkeypatch.delenv("VIBONY_DEFAULT_MODEL", raising=False)
    response = await client.post("/sessions", json={"task_id": "task-01-training-loop"})
    assert response.status_code == 400
    assert "VIBONY_DEFAULT_MODEL" in response.json()["detail"]


# ---------------------------------------------------------------------------
# POST /sessions — async mode + time_limit_seconds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_create_async_session_success(authed_client):
    client, _ = authed_client
    payload = dict(VALID_PAYLOAD, mode="async", time_limit_seconds=3600)
    response = await client.post("/sessions", json=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["mode"] == "async"
    assert body["time_limit_seconds"] == 3600
    code = body["session_code"]

    # Round-trips through GET /sessions/{code} (interviewer view).
    got = await client.get(f"/sessions/{code}")
    assert got.status_code == 200
    assert got.json()["mode"] == "async"
    assert got.json()["time_limit_seconds"] == 3600


@pytest.mark.asyncio(loop_scope="session")
async def test_create_async_session_requires_time_limit(authed_client):
    client, _ = authed_client
    payload = dict(VALID_PAYLOAD, mode="async")
    response = await client.post("/sessions", json=payload)
    assert response.status_code == 422, response.text


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("seconds", [60, 5 * 60 - 1, 4 * 60 * 60 + 1, 99999])
async def test_create_async_session_time_limit_out_of_range(authed_client, seconds):
    client, _ = authed_client
    payload = dict(VALID_PAYLOAD, mode="async", time_limit_seconds=seconds)
    response = await client.post("/sessions", json=payload)
    assert response.status_code == 422, response.text


@pytest.mark.asyncio(loop_scope="session")
async def test_create_live_session_rejects_time_limit(authed_client):
    client, _ = authed_client
    payload = dict(VALID_PAYLOAD, mode="live", time_limit_seconds=3600)
    response = await client.post("/sessions", json=payload)
    assert response.status_code == 422, response.text


@pytest.mark.asyncio(loop_scope="session")
async def test_list_sessions_reflects_mode(authed_client):
    client, _ = authed_client
    await client.post("/sessions", json=VALID_PAYLOAD)
    await client.post(
        "/sessions", json=dict(VALID_PAYLOAD, mode="async", time_limit_seconds=1800)
    )

    response = await client.get("/sessions")
    assert response.status_code == 200
    sessions = response.json()["sessions"]
    modes = {s["mode"] for s in sessions}
    assert modes == {"live", "async"}
    async_row = next(s for s in sessions if s["mode"] == "async")
    assert async_row["time_limit_seconds"] == 1800


# ---------------------------------------------------------------------------
# GET /sessions — tenant-scoped list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_list_sessions_requires_auth(http_client: AsyncClient):
    response = await http_client.get("/sessions")
    assert response.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
async def test_list_sessions_empty(authed_client):
    client, _ = authed_client
    response = await client.get("/sessions")
    assert response.status_code == 200
    assert response.json()["sessions"] == []


@pytest.mark.asyncio(loop_scope="session")
async def test_list_sessions_returns_own_sessions(authed_client):
    client, _ = authed_client
    await client.post("/sessions", json=VALID_PAYLOAD)
    await client.post("/sessions", json=VALID_PAYLOAD)

    response = await client.get("/sessions")
    assert response.status_code == 200
    sessions = response.json()["sessions"]
    assert len(sessions) == 2
    for s in sessions:
        assert s["task_id"] == "task-01-training-loop"


@pytest.mark.asyncio(loop_scope="session")
async def test_list_sessions_cross_tenant_isolation(http_client: AsyncClient):
    """Sessions of company A are not visible to a user from company B."""
    # User A
    await http_client.post("/auth/signup", json={
        "email": "user_a@example.com", "password": "password123", "company_name": "Company A"
    })
    await http_client.post("/sessions", json=VALID_PAYLOAD)

    # Log out, sign up as user B
    await http_client.post("/auth/logout")
    await http_client.post("/auth/signup", json={
        "email": "user_b@example.com", "password": "password123", "company_name": "Company B"
    })

    response = await http_client.get("/sessions")
    assert response.status_code == 200
    assert response.json()["sessions"] == []


# ---------------------------------------------------------------------------
# GET /sessions/{code} — candidate bootstrap (requires ?candidate_token=)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_get_session_no_token_returns_401(http_client: AsyncClient):
    response = await http_client.get("/sessions/AAAA-AAAA")
    assert response.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
async def test_get_session_unknown_code_with_token_returns_401(http_client: AsyncClient):
    """The candidate-token path must not be a session-code existence oracle —
    an unknown code returns the same 401 as a wrong token, never 404."""
    response = await http_client.get("/sessions/AAAA-AAAA?candidate_token=some-token")
    assert response.status_code == 401
    assert response.json()["detail"]["reason"] == "unauthorized"


@pytest.mark.asyncio(loop_scope="session")
async def test_get_session_bad_token_returns_401(authed_client):
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]
    response = await client.get(f"/sessions/{code}?candidate_token=wrong-token")
    assert response.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
async def test_get_session_returns_config(authed_client):
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code, token = created["session_code"], created["candidate_token"]

    response = await client.get(f"/sessions/{code}?candidate_token={token}")
    assert response.status_code == 200
    body = response.json()
    assert body.get("candidate_token") is None  # candidate path must not echo the token back
    assert body["session_code"] == code
    assert body["task_dir_name"] == "training-loop"
    # ws_token is set to candidate_token for CLI bootstrap
    assert body["ws_token"] == token


@pytest.mark.asyncio(loop_scope="session")
async def test_get_session_interviewer_returns_candidate_token(authed_client):
    """Cookie-auth path returns candidate_token so the interviewer can always find the link."""
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code, token = created["session_code"], created["candidate_token"]

    response = await client.get(f"/sessions/{code}")
    assert response.status_code == 200
    body = response.json()
    assert body["candidate_token"] == token


@pytest.mark.asyncio(loop_scope="session")
async def test_get_session_completed_returns_410(authed_client):
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code, token = created["session_code"], created["candidate_token"]

    await client.post(f"/sessions/{code}/end")
    response = await client.get(f"/sessions/{code}?candidate_token={token}")
    assert response.status_code == 410
    assert response.json()["detail"]["reason"] == "completed"


# ---------------------------------------------------------------------------
# POST /sessions/{code}/end — cookie auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_end_session_idempotent(authed_client):
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]

    first = await client.post(f"/sessions/{code}/end")
    assert first.status_code == 200
    assert first.json()["session_status"] == "completed"

    second = await client.post(f"/sessions/{code}/end")
    assert second.status_code == 200
    assert second.json()["session_status"] == "completed"


@pytest.mark.asyncio(loop_scope="session")
async def test_end_session_requires_auth(http_client: AsyncClient):
    response = await http_client.post("/sessions/AAAA-AAAA/end")
    assert response.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
async def test_end_session_cross_tenant_returns_403(http_client: AsyncClient):
    """User from company B cannot end a session belonging to company A."""
    # Create session as user A
    await http_client.post("/auth/signup", json={
        "email": "end_a@example.com", "password": "password123", "company_name": "EndCo A"
    })
    created = (await http_client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]

    # Switch to user B
    await http_client.post("/auth/logout")
    await http_client.post("/auth/signup", json={
        "email": "end_b@example.com", "password": "password123", "company_name": "EndCo B"
    })

    response = await http_client.post(f"/sessions/{code}/end")
    assert response.status_code == 403


@pytest.mark.asyncio(loop_scope="session")
async def test_end_session_not_found(authed_client):
    client, _ = authed_client
    response = await client.post("/sessions/AAAA-AAAA/end")
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "not_found"


@pytest.mark.asyncio(loop_scope="session")
async def test_end_session_unknown_code_with_token_returns_401(http_client: AsyncClient):
    """The candidate-token /end path is not a session-code existence oracle —
    an unknown code returns 401 (same as a wrong token), not 404."""
    response = await http_client.post("/sessions/AAAA-AAAA/end?candidate_token=some-token")
    assert response.status_code == 401
    assert response.json()["detail"]["reason"] == "unauthorized"


# ---------------------------------------------------------------------------
# REST contract & data correctness
# ---------------------------------------------------------------------------


async def _activate_session(db_engine, code: str) -> None:
    """Flip a freshly-created (pending) session to active so /end emits session_end."""
    sm = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    async with sm() as s:
        row = (
            await s.execute(select(DbSession).where(DbSession.code == code))
        ).scalar_one()
        row.status = "active"
        await s.commit()


async def _read_session_end(db_engine, code: str) -> tuple[str | None, dict]:
    """Return (row.completed_reason, session_end event payload) for an ended session."""
    sm = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    async with sm() as s:
        row = (
            await s.execute(select(DbSession).where(DbSession.code == code))
        ).scalar_one()
        end_ev = (
            await s.execute(
                select(Event).where(
                    Event.session_id == row.id, Event.type == "session_end"
                )
            )
        ).scalar_one()
        return row.completed_reason, end_ev.payload


@pytest.mark.asyncio(loop_scope="session")
async def test_end_session_candidate_reason_is_candidate_quit(authed_client, db_engine):
    """A candidate-initiated /end records reason='candidate_quit' on both the
    row and the persisted/broadcast session_end event — not a hardcoded 'interviewer'
    (and not the bogus 'candidate' that isn't in the protocol enum)."""
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code, token = created["session_code"], created["candidate_token"]
    await _activate_session(db_engine, code)

    with patch("vibony_backend.assessment.job.enqueue", new_callable=AsyncMock):
        resp = await client.post(f"/sessions/{code}/end?candidate_token={token}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["session_status"] == "completed"

    completed_reason, payload = await _read_session_end(db_engine, code)
    assert completed_reason == "candidate_quit"
    assert payload["reason"] == "candidate_quit"


@pytest.mark.asyncio(loop_scope="session")
async def test_end_session_interviewer_reason_unchanged(authed_client, db_engine):
    """Regression guard: an interviewer (cookie) /end still records 'interviewer'."""
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]
    await _activate_session(db_engine, code)

    with patch("vibony_backend.assessment.job.enqueue", new_callable=AsyncMock):
        resp = await client.post(f"/sessions/{code}/end")
    assert resp.status_code == 200, resp.text

    completed_reason, payload = await _read_session_end(db_engine, code)
    assert completed_reason == "interviewer"
    assert payload["reason"] == "interviewer"


@pytest.mark.asyncio(loop_scope="session")
async def test_create_session_unusable_private_task_returns_400(authed_client, db_engine):
    """A private task in the caller's own workspace but authored by someone
    else is not usable — resolution returns a uniform 400 unknown_task, not a 403 that
    would confirm the task exists (the not-found-hiding contract)."""
    client, company_id = authed_client
    sm = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    async with sm() as s:
        other = User(email="other_author@example.com", password_hash="x")
        s.add(other)
        await s.flush()
        s.add(
            Task(
                company_id=uuid.UUID(company_id),
                created_by=other.id,
                slug="private-foo",
                title="Private Foo",
                description_md="",
                starter_files=[],
                runtime_image=DEFAULT_TEMPLATE_IMAGE,
                weakness={},
                spec={},
                visibility="private",
                is_template=False,
            )
        )
        await s.commit()

    resp = await client.post("/sessions", json=dict(VALID_PAYLOAD, task_id="private-foo"))
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["reason"] == "unknown_task"


@pytest.mark.asyncio(loop_scope="session")
async def test_create_session_rejects_non_allowlisted_fallback(authed_client):
    """A fallback_models entry outside the allowlist → 422 (validated like
    primary_model; the success fixture's fallback is allowlisted in conftest)."""
    client, _ = authed_client
    bad = dict(
        VALID_PAYLOAD,
        ai_config={
            "primary_model": "deepseek/deepseek-chat",
            "fallback_models": ["anthropic/claude-not-allowed"],
        },
    )
    resp = await client.post("/sessions", json=bad)
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio(loop_scope="session")
async def test_get_session_malformed_ai_config_does_not_500(authed_client, db_engine):
    """A legacy session with a malformed ai_config JSONB (no primary_model)
    is served with a coerced config, not a 500 on response serialization."""
    client, company_id = authed_client
    sm = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    async with sm() as s:
        s.add(
            DbSession(
                code="MALF0001",
                status="completed",
                task_id="task-01-training-loop",
                protocol_version=PROTOCOL_VERSION,
                ai_config={},  # missing primary_model
                candidate_token=str(uuid.uuid4()),
                company_id=uuid.UUID(company_id),
                expires_at=datetime.now(UTC) + timedelta(hours=8),
            )
        )
        await s.commit()

    resp = await client.get("/sessions/MALF0001")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ai_config"]["primary_model"] == ""


@pytest.mark.asyncio(loop_scope="session")
async def test_get_session_malformed_fallback_models_does_not_500(authed_client, db_engine):
    """Follow-up: a valid primary_model but non-string fallback_models
    items must be coerced, not 500 the read path on AIConfig's list[str] validator
    (the first pass only guarded primary_model)."""
    client, company_id = authed_client
    sm = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    async with sm() as s:
        s.add(
            DbSession(
                code="MALF0002",
                status="completed",
                task_id="task-01-training-loop",
                protocol_version=PROTOCOL_VERSION,
                ai_config={"primary_model": "openrouter/x", "fallback_models": [123, "ok", None]},
                candidate_token=str(uuid.uuid4()),
                company_id=uuid.UUID(company_id),
                expires_at=datetime.now(UTC) + timedelta(hours=8),
            )
        )
        await s.commit()

    resp = await client.get("/sessions/MALF0002")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ai_config"]["primary_model"] == "openrouter/x"
    assert body["ai_config"]["fallback_models"] == ["ok"]


# ---------------------------------------------------------------------------
# GET /sessions/{code}/events — cookie auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_events_requires_auth(http_client: AsyncClient):
    response = await http_client.get("/sessions/AAAA-AAAA/events")
    assert response.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
async def test_events_empty(authed_client):
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]
    response = await client.get(f"/sessions/{code}/events")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio(loop_scope="session")
async def test_events_replay_filters_by_since(authed_client, db_engine):
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]

    sm = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    async with sm() as s:
        row = (
            await s.execute(select(DbSession).where(DbSession.code == code))
        ).scalar_one()
        for seq, type_ in [(1, "session_start"), (2, "user_message"), (3, "assistant_message")]:
            s.add(
                Event(
                    session_id=row.id,
                    seq=seq,
                    id=uuid.uuid4(),
                    type=type_,
                    client_timestamp=datetime.now(UTC),
                    payload={"seq": seq},
                )
            )
        await s.commit()

    all_resp = await client.get(f"/sessions/{code}/events")
    assert all_resp.status_code == 200
    body = all_resp.json()
    assert [e["seq"] for e in body] == [1, 2, 3]
    assert [e["type"] for e in body] == ["session_start", "user_message", "assistant_message"]
    assert all(e["session_code"] == code for e in body)

    tail_resp = await client.get(f"/sessions/{code}/events?since=1")
    assert [e["seq"] for e in tail_resp.json()] == [2, 3]

    nothing_resp = await client.get(f"/sessions/{code}/events?since=10")
    assert nothing_resp.json() == []


@pytest.mark.asyncio(loop_scope="session")
async def test_events_cross_tenant_returns_403(http_client: AsyncClient):
    """User from company B cannot read events of a session belonging to company A."""
    await http_client.post("/auth/signup", json={
        "email": "ev_a@example.com", "password": "password123", "company_name": "EvCo A"
    })
    created = (await http_client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]

    await http_client.post("/auth/logout")
    await http_client.post("/auth/signup", json={
        "email": "ev_b@example.com", "password": "password123", "company_name": "EvCo B"
    })

    response = await http_client.get(f"/sessions/{code}/events")
    assert response.status_code == 403


@pytest.mark.asyncio(loop_scope="session")
async def test_events_legacy_no_company_access(http_client: AsyncClient, db_engine):
    """A legacy (company_id IS NULL) session is readable by the interviewer
    who created it (was a wrong 401 → dead-access); a different authenticated user
    gets 403 (authorization failure), not 401 (which means unauthenticated)."""
    await http_client.post("/auth/signup", json={
        "email": "legacy_a@example.com", "password": "password123", "company_name": "LegacyCo A"
    })

    sm = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    async with sm() as s:
        user_a = (
            await s.execute(select(User).where(User.email == "legacy_a@example.com"))
        ).scalar_one()
        row = DbSession(
            code="LEGACY01",
            status="completed",
            task_id="task-01-training-loop",
            protocol_version=PROTOCOL_VERSION,
            ai_config={"primary_model": "m", "fallback_models": []},
            candidate_token=str(uuid.uuid4()),
            company_id=None,
            created_by=user_a.id,
            expires_at=datetime.now(UTC) + timedelta(hours=8),
        )
        s.add(row)
        await s.flush()
        s.add(
            Event(
                session_id=row.id,
                seq=1,
                id=uuid.uuid4(),
                type="session_start",
                client_timestamp=datetime.now(UTC),
                payload={},
            )
        )
        await s.commit()

    # The creator reads the legacy session's events.
    resp = await http_client.get("/sessions/LEGACY01/events")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1

    # A different authenticated user is denied with 403 (not 401).
    await http_client.post("/auth/logout")
    await http_client.post("/auth/signup", json={
        "email": "legacy_b@example.com", "password": "password123", "company_name": "LegacyCo B"
    })
    resp_other = await http_client.get("/sessions/LEGACY01/events")
    assert resp_other.status_code == 403


# ---------------------------------------------------------------------------
# GET /sessions/{code}/bundle.tar.gz — candidate_token required
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_bundle_no_token_returns_401(authed_client):
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]
    response = await client.get(f"/sessions/{code}/bundle.tar.gz")
    assert response.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
async def test_bundle_bad_token_returns_401(authed_client):
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]
    response = await client.get(f"/sessions/{code}/bundle.tar.gz?candidate_token=wrong")
    assert response.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
async def test_bundle_unknown_code_returns_401(http_client: AsyncClient):
    """The bundle's candidate-token path is not a session-code oracle —
    an unknown code returns 401 (same as a wrong token), not 404."""
    response = await http_client.get(
        "/sessions/AAAA-AAAA/bundle.tar.gz?candidate_token=some-token"
    )
    assert response.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
async def test_bundle_valid_token_returns_200(authed_client):
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code, token = created["session_code"], created["candidate_token"]
    response = await client.get(f"/sessions/{code}/bundle.tar.gz?candidate_token={token}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/gzip"


# ---------------------------------------------------------------------------
# Chat proxy — still uses candidate_token Bearer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_chat_proxy_410_on_completed_session(authed_client, monkeypatch):
    from types import SimpleNamespace

    async def fake_acompletion(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="should not be reached", tool_calls=None),
                )
            ],
            usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 0}),
        )

    monkeypatch.setattr("vibony_backend.proxy.litellm.acompletion", fake_acompletion)

    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code, token = created["session_code"], created["candidate_token"]
    await client.post(f"/sessions/{code}/end")

    resp = await client.post(
        "/v1/chat",
        json={
            "task_id": "task-01-training-loop",
            "messages": [{"role": "user", "content": "ping"}],
            "session_code": code,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 410
    assert resp.json()["detail"]["reason"] == "completed"


@pytest.mark.asyncio(loop_scope="session")
async def test_chat_proxy_410_on_unknown_session(http_client: AsyncClient, monkeypatch):
    from types import SimpleNamespace

    async def fake_acompletion(**kwargs):
        return SimpleNamespace(choices=[], usage=SimpleNamespace(model_dump=lambda: {}))

    monkeypatch.setattr("vibony_backend.proxy.litellm.acompletion", fake_acompletion)

    resp = await http_client.post(
        "/v1/chat",
        json={
            "task_id": "task-01-training-loop",
            "messages": [{"role": "user", "content": "ping"}],
            "session_code": "ZZZZ-ZZZZ",
        },
        headers={"Authorization": "Bearer whatever"},
    )
    assert resp.status_code == 410
    assert resp.json()["detail"] == {"reason": "completed"}


@pytest.mark.asyncio(loop_scope="session")
async def test_chat_proxy_401_without_bearer(authed_client, monkeypatch):
    from types import SimpleNamespace

    async def fake_acompletion(**kwargs):  # pragma: no cover
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="no", tool_calls=None))],
            usage=SimpleNamespace(model_dump=lambda: {}),
        )

    monkeypatch.setattr("vibony_backend.proxy.litellm.acompletion", fake_acompletion)

    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]

    resp = await client.post(
        "/v1/chat",
        json={
            "task_id": "task-01-training-loop",
            "messages": [{"role": "user", "content": "ping"}],
            "session_code": code,
        },
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == {"reason": "unauthorized"}


@pytest.mark.asyncio(loop_scope="session")
async def test_chat_proxy_403_with_wrong_bearer(authed_client, monkeypatch):
    from types import SimpleNamespace

    async def fake_acompletion(**kwargs):  # pragma: no cover
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="x", tool_calls=None))],
            usage=SimpleNamespace(model_dump=lambda: {}),
        )

    monkeypatch.setattr("vibony_backend.proxy.litellm.acompletion", fake_acompletion)

    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]

    resp = await client.post(
        "/v1/chat",
        json={
            "task_id": "task-01-training-loop",
            "messages": [{"role": "user", "content": "ping"}],
            "session_code": code,
        },
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == {"reason": "unauthorized"}


@pytest.mark.asyncio(loop_scope="session")
async def test_chat_proxy_skips_gate_in_local_mode(http_client: AsyncClient, monkeypatch):
    from types import SimpleNamespace

    async def fake_acompletion(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="ok", tool_calls=None),
                )
            ],
            usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 1}),
        )

    monkeypatch.setattr("vibony_backend.proxy.litellm.acompletion", fake_acompletion)

    resp = await http_client.post(
        "/v1/chat",
        json={
            "task_id": "task-01-training-loop",
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Files endpoints — cookie auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_files_401_without_cookie(http_client: AsyncClient):
    resp = await http_client.get("/sessions/AAAA-AAAA/files")
    assert resp.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
async def test_files_503_when_sandbox_unavailable(authed_client, monkeypatch):
    import vibony_backend.sessions.rest as rest_module

    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]

    monkeypatch.setattr(rest_module.web_fs_proxy_registry, "get", lambda _: None)
    resp = await client.get(f"/sessions/{code}/files")
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "sandbox_unavailable"


@pytest.mark.asyncio(loop_scope="session")
async def test_file_content_503_when_sandbox_unavailable(authed_client, monkeypatch):
    import vibony_backend.sessions.rest as rest_module

    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]

    monkeypatch.setattr(rest_module.web_fs_proxy_registry, "get", lambda _: None)
    resp = await client.get(f"/sessions/{code}/files/content?path=train.py")
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "sandbox_unavailable"


@pytest.mark.asyncio(loop_scope="session")
async def test_file_content_path_traversal_rejected(authed_client):
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]

    for bad_path in ["../etc/passwd", "/etc/passwd", "../../secrets"]:
        resp = await client.get(f"/sessions/{code}/files/content?path={bad_path}")
        assert resp.status_code == 400, f"expected 400 for path={bad_path!r}, got {resp.status_code}"


# ---------------------------------------------------------------------------
# share_token + replay endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_create_share_token_success(authed_client):
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]

    # A share token is only minted for a completed session.
    await client.post(f"/sessions/{code}/end")
    resp = await client.post(f"/sessions/{code}/share")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["share_token"]) > 0
    assert body["expires_at"] is not None  # stamped at mint


@pytest.mark.asyncio(loop_scope="session")
async def test_create_share_token_active_session_returns_409(authed_client):
    """Minting a share token for a non-completed session is rejected."""
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]

    resp = await client.post(f"/sessions/{code}/share")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "not_completed"


@pytest.mark.asyncio(loop_scope="session")
async def test_create_share_token_idempotent(authed_client):
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]
    await client.post(f"/sessions/{code}/end")

    r1 = await client.post(f"/sessions/{code}/share")
    r2 = await client.post(f"/sessions/{code}/share")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["share_token"] == r2.json()["share_token"]


@pytest.mark.asyncio(loop_scope="session")
async def test_revoke_share_token(authed_client):
    """DELETE /share nulls the token → outstanding replay links stop working."""
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]
    await client.post(f"/sessions/{code}/end")

    share_token = (await client.post(f"/sessions/{code}/share")).json()["share_token"]
    # The link works before revoke.
    assert (
        await client.get(f"/sessions/{code}/replay?share_token={share_token}")
    ).status_code == 200

    revoke = await client.delete(f"/sessions/{code}/share")
    assert revoke.status_code == 204

    # The same token no longer resolves (uniform 403, same as a wrong token).
    after = await client.get(f"/sessions/{code}/replay?share_token={share_token}")
    assert after.status_code == 403
    # Idempotent: a second revoke is a no-op.
    assert (await client.delete(f"/sessions/{code}/share")).status_code == 204


@pytest.mark.asyncio(loop_scope="session")
async def test_create_share_token_no_auth(http_client: AsyncClient):
    resp = await http_client.post("/sessions/AAAA-AAAA/share")
    assert resp.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
async def test_create_share_token_cross_tenant(http_client: AsyncClient):
    await http_client.post("/auth/signup", json={
        "email": "share_a@example.com", "password": "password123", "company_name": "ShareCo A"
    })
    created = (await http_client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]

    await http_client.post("/auth/logout")
    await http_client.post("/auth/signup", json={
        "email": "share_b@example.com", "password": "password123", "company_name": "ShareCo B"
    })

    resp = await http_client.post(f"/sessions/{code}/share")
    assert resp.status_code == 403


@pytest.mark.asyncio(loop_scope="session")
async def test_replay_config_completed_session(authed_client, db_engine):
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]

    await client.post(f"/sessions/{code}/end")
    share_resp = await client.post(f"/sessions/{code}/share")
    share_token = share_resp.json()["share_token"]

    resp = await client.get(f"/sessions/{code}/replay?share_token={share_token}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_code"] == code
    assert body["session_status"] == "completed"


@pytest.mark.asyncio(loop_scope="session")
async def test_replay_config_wrong_share_token(authed_client, db_engine):
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]

    await client.post(f"/sessions/{code}/end")
    await client.post(f"/sessions/{code}/share")

    resp = await client.get(f"/sessions/{code}/replay?share_token=wrong-token")
    assert resp.status_code == 403


@pytest.mark.asyncio(loop_scope="session")
async def test_replay_config_no_share_token_generated(authed_client, db_engine):
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]
    await client.post(f"/sessions/{code}/end")

    resp = await client.get(f"/sessions/{code}/replay?share_token=any-token")
    assert resp.status_code == 403


@pytest.mark.asyncio(loop_scope="session")
async def test_replay_config_expired_share_token(authed_client, db_engine):
    """A past share_token_expires_at rejects the link like an invalid token."""
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]
    await client.post(f"/sessions/{code}/end")
    share_token = (await client.post(f"/sessions/{code}/share")).json()["share_token"]

    # Backdate the expiry directly.
    sm = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    async with sm() as s:
        row = (
            await s.execute(select(DbSession).where(DbSession.code == code))
        ).scalar_one()
        row.share_token_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()

    resp = await client.get(f"/sessions/{code}/replay?share_token={share_token}")
    assert resp.status_code == 403


@pytest.mark.asyncio(loop_scope="session")
async def test_replay_config_not_found(http_client: AsyncClient):
    resp = await http_client.get("/sessions/XXXX-XXXX/replay?share_token=any")
    assert resp.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
async def test_events_negative_since_rejected(authed_client):
    """A negative ?since= is rejected (422), not silently treated as 'all'."""
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]
    resp = await client.get(f"/sessions/{code}/events?since=-1")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# by-candidate-token endpoint + candidate_url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_create_session_returns_candidate_url(authed_client):
    """POST /sessions returns a non-null candidate_url (every session is web)."""
    client, _ = authed_client
    resp = await client.post("/sessions", json=VALID_PAYLOAD)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    candidate_url = body.get("candidate_url")
    assert candidate_url is not None
    # URL format: <origin>/candidate?token=<candidate_token>
    assert body["candidate_token"] in candidate_url
    assert "candidate" in candidate_url


@pytest.mark.asyncio(loop_scope="session")
async def test_by_candidate_token_returns_session_config(authed_client):
    """GET /sessions/by-candidate-token/{token} returns SessionConfig."""
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    token = created["candidate_token"]

    # No auth required — public endpoint.
    resp = await client.get(f"/sessions/by-candidate-token/{token}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_code"] == created["session_code"]
    assert body["task_id"] == "task-01-training-loop"


@pytest.mark.asyncio(loop_scope="session")
async def test_by_candidate_token_invalid_token_returns_401(http_client: AsyncClient):
    # Invalid token → 401 (consistent with WS 4401; avoids leaking token existence).
    resp = await http_client.get("/sessions/by-candidate-token/nonexistent-token")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Bug regression: candidate_url built from VIBONY_FRONTEND_URL (not request host)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_candidate_url_uses_vibony_frontend_url(authed_client):
    """candidate_url must use VIBONY_FRONTEND_URL, not the HTTP request host.

    Regression: before fix, the URL was built from request.base_url which
    returned the Docker-internal hostname (http://backend:8000/...) instead of
    the user-facing frontend URL.
    """
    import os
    from unittest.mock import patch

    client, _ = authed_client
    with patch.dict(os.environ, {"VIBONY_FRONTEND_URL": "https://app.example.com"}):
        resp = await client.post("/sessions", json=VALID_PAYLOAD)

    assert resp.status_code == 201, resp.text
    url = resp.json()["candidate_url"]
    assert url is not None
    assert url.startswith("https://app.example.com"), (
        f"candidate_url should use VIBONY_FRONTEND_URL, got: {url}"
    )
    assert "/candidate?token=" in url


# ---------------------------------------------------------------------------
# Bug regression: candidate can end their own session via ?candidate_token=
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_end_session_by_candidate_token(authed_client, http_client: AsyncClient):
    """Candidate-initiated end via ?candidate_token= must return 200.

    Regression: before fix, POST /sessions/{code}/end only accepted cookie auth
    (Depends(current_company)), so the candidate always got 401.
    """
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]
    token = created["candidate_token"]

    # http_client has no session cookies → simulates anonymous candidate request.
    resp = await http_client.post(f"/sessions/{code}/end?candidate_token={token}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["session_status"] == "completed"


@pytest.mark.asyncio(loop_scope="session")
async def test_end_session_wrong_candidate_token_returns_401(authed_client, http_client: AsyncClient):
    """Wrong candidate_token must return 401, not 200 or 403.

    Regression: before auth check was added before session load, this returned
    404 for non-existent sessions when no auth was provided.
    """
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]

    resp = await http_client.post(f"/sessions/{code}/end?candidate_token=wrong-token")
    assert resp.status_code == 401, resp.text


# -------- /end enqueues the assessment job (live path) --------


@pytest.mark.asyncio(loop_scope="session")
async def test_end_active_session_enqueues_assessment(authed_client, db_engine):
    """Ending a genuinely-active session fires the assessment enqueue (was_active)."""
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]

    sm = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    async with sm() as s:
        row = (
            await s.execute(select(DbSession).where(DbSession.code == code))
        ).scalar_one()
        row.status = "active"
        sid = row.id
        await s.commit()

    spy = AsyncMock(return_value=None)
    with patch("vibony_backend.assessment.job.enqueue", new=spy):
        resp = await client.post(f"/sessions/{code}/end")
    assert resp.status_code == 200, resp.text
    spy.assert_awaited_once_with(sid, code)


@pytest.mark.asyncio(loop_scope="session")
async def test_end_pending_session_skips_assessment(authed_client):
    """A pending session that never started has nothing to assess → no enqueue."""
    client, _ = authed_client
    created = (await client.post("/sessions", json=VALID_PAYLOAD)).json()
    code = created["session_code"]

    spy = AsyncMock(return_value=None)
    with patch("vibony_backend.assessment.job.enqueue", new=spy):
        resp = await client.post(f"/sessions/{code}/end")
    assert resp.status_code == 200, resp.text
    spy.assert_not_awaited()
