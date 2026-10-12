"""Tests: assessment REST endpoints (GET + regenerate).

Auth matrix (interviewer membership only): member cookie 200; no cookie 401;
cross-tenant 403; candidate_token / share_token never accepted. Plus the status
lifecycle (not_started / ready / failed) and regenerate reset (job spawn patched
so no real LLM call).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vibony_backend.db.models import Event
from vibony_backend.db.models import Session as DbSession
from vibony_backend.db.models import SessionAssessment

VALID_PAYLOAD = {
    "task_id": "task-01-training-loop",
    "ai_config": {
        "primary_model": "deepseek/deepseek-chat",
        "fallback_models": [],
    },
}

_session_loop = pytest.mark.asyncio(loop_scope="session")


async def _session_id(db_engine, code: str) -> uuid.UUID:
    sm = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    async with sm() as s:
        return (
            await s.execute(select(DbSession.id).where(DbSession.code == code))
        ).scalar_one()


async def _seed_assessment(db_engine, session_id: uuid.UUID, **fields) -> None:
    sm = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    async with sm() as s:
        s.add(SessionAssessment(session_id=session_id, **fields))
        await s.commit()


async def _mark_completed(db_engine, session_id: uuid.UUID) -> None:
    sm = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    async with sm() as s:
        row = await s.get(DbSession, session_id)
        row.status = "completed"
        await s.commit()


async def _seed_event(db_engine, session_id: uuid.UUID, seq: int = 1) -> None:
    """Seed one transcript event so the session reads as having been active.

    In production a session with an assessment always has events (store_event
    claims pending→active on the first); the regenerate gate relies on that.
    """
    sm = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    async with sm() as s:
        s.add(
            Event(
                session_id=session_id,
                seq=seq,
                id=uuid.uuid4(),
                type="session_start",
                client_timestamp=datetime.now(UTC),
                payload={},
            )
        )
        await s.commit()


# ---------------------------------------------------------------------------
# GET /sessions/{code}/assessment
# ---------------------------------------------------------------------------


@_session_loop
async def test_get_assessment_not_started(authed_client):
    client, _ = authed_client
    code = (await client.post("/sessions", json=VALID_PAYLOAD)).json()["session_code"]

    resp = await client.get(f"/sessions/{code}/assessment")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "not_started"
    assert body["findings"] == []
    assert body["summary_md"] is None


@_session_loop
async def test_get_assessment_ready(authed_client, db_engine):
    client, _ = authed_client
    code = (await client.post("/sessions", json=VALID_PAYLOAD)).json()["session_code"]
    sid = await _session_id(db_engine, code)
    await _seed_assessment(
        db_engine,
        sid,
        status="ready",
        model="deepseek/deepseek-chat",
        summary_md="## What went well\nGood checkpointing.",
        findings=[
            {
                "category": "gap",
                "title": "No optimizer state",
                "detail": "Checkpoint saved only model weights.",
                "severity": "blocking",
                "event_seq": 7,
            }
        ],
    )

    resp = await client.get(f"/sessions/{code}/assessment")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ready"
    assert body["model"] == "deepseek/deepseek-chat"
    assert body["summary_md"].startswith("## What went well")
    assert len(body["findings"]) == 1
    assert body["findings"][0]["category"] == "gap"
    assert body["findings"][0]["severity"] == "blocking"
    assert body["findings"][0]["event_seq"] == 7


@_session_loop
async def test_get_assessment_failed(authed_client, db_engine):
    client, _ = authed_client
    code = (await client.post("/sessions", json=VALID_PAYLOAD)).json()["session_code"]
    sid = await _session_id(db_engine, code)
    await _seed_assessment(db_engine, sid, status="failed", error="llm timeout")

    resp = await client.get(f"/sessions/{code}/assessment")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    assert body["error"] == "llm timeout"
    assert body["findings"] == []


@_session_loop
async def test_get_assessment_requires_cookie(http_client: AsyncClient):
    """No cookie → 401 (a candidate_token query param is never honored here)."""
    resp = await http_client.get("/sessions/AAAA-AAAA/assessment")
    assert resp.status_code == 401

    resp2 = await http_client.get(
        "/sessions/AAAA-AAAA/assessment?candidate_token=whatever"
    )
    assert resp2.status_code == 401

    resp3 = await http_client.get(
        "/sessions/AAAA-AAAA/assessment?share_token=whatever"
    )
    assert resp3.status_code == 401


@_session_loop
async def test_get_assessment_cross_tenant_403(http_client: AsyncClient):
    """User from company B cannot read company A's assessment."""
    await http_client.post("/auth/signup", json={
        "email": "asmt_a@example.com", "password": "password123", "company_name": "Asmt A"
    })
    code = (await http_client.post("/sessions", json=VALID_PAYLOAD)).json()["session_code"]

    await http_client.post("/auth/logout")
    await http_client.post("/auth/signup", json={
        "email": "asmt_b@example.com", "password": "password123", "company_name": "Asmt B"
    })

    resp = await http_client.get(f"/sessions/{code}/assessment")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /sessions/{code}/assessment/regenerate
# ---------------------------------------------------------------------------


@_session_loop
async def test_regenerate_resets_to_pending_and_spawns(authed_client, db_engine):
    client, _ = authed_client
    code = (await client.post("/sessions", json=VALID_PAYLOAD)).json()["session_code"]
    sid = await _session_id(db_engine, code)
    await _mark_completed(db_engine, sid)  # regenerate gated on completed
    await _seed_event(db_engine, sid)  # gated on the session having a transcript
    await _seed_assessment(
        db_engine,
        sid,
        status="ready",
        model="old-model",
        summary_md="old",
        findings=[{"category": "gap", "title": "old", "detail": "d"}],
    )

    # Patch the registry spawn so regenerate performs the real DB reset but does
    # not launch the LLM job. (regenerate spawns via this exact singleton method.)
    spy = AsyncMock(return_value=None)
    with patch(
        "vibony_backend.assessment.job.assessment_job_registry.spawn", new=spy
    ):
        resp = await client.post(f"/sessions/{code}/assessment/regenerate")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["findings"] == []
    assert body["summary_md"] is None
    assert body["model"] is None
    spy.assert_awaited_once()


@_session_loop
async def test_regenerate_rejects_non_completed(authed_client, db_engine):
    """A live/never-completed session cannot be (re)assessed → 409. No job
    is spawned (no LLM cost on an incomplete transcript)."""
    client, _ = authed_client
    code = (await client.post("/sessions", json=VALID_PAYLOAD)).json()["session_code"]

    spy = AsyncMock(return_value=None)
    with patch(
        "vibony_backend.assessment.job.assessment_job_registry.spawn", new=spy
    ):
        resp = await client.post(f"/sessions/{code}/assessment/regenerate")

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["reason"] == "not_completed"
    spy.assert_not_awaited()


@_session_loop
async def test_regenerate_rejects_empty_completed_session(authed_client, db_engine):
    """A completed session that never went active (no candidate ever
    connected → zero events → empty transcript) has nothing to assess. Mirrors the
    was_active auto-enqueue gate → 409 empty_session, no LLM job spawned."""
    client, _ = authed_client
    code = (await client.post("/sessions", json=VALID_PAYLOAD)).json()["session_code"]
    sid = await _session_id(db_engine, code)
    await _mark_completed(db_engine, sid)  # completed, but no events seeded

    spy = AsyncMock(return_value=None)
    with patch(
        "vibony_backend.assessment.job.assessment_job_registry.spawn", new=spy
    ):
        resp = await client.post(f"/sessions/{code}/assessment/regenerate")

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["reason"] == "empty_session"
    spy.assert_not_awaited()


@_session_loop
async def test_regenerate_requires_cookie(http_client: AsyncClient):
    resp = await http_client.post("/sessions/AAAA-AAAA/assessment/regenerate")
    assert resp.status_code == 401


@_session_loop
async def test_regenerate_cross_tenant_403(http_client: AsyncClient):
    await http_client.post("/auth/signup", json={
        "email": "rgn_a@example.com", "password": "password123", "company_name": "Rgn A"
    })
    code = (await http_client.post("/sessions", json=VALID_PAYLOAD)).json()["session_code"]

    await http_client.post("/auth/logout")
    await http_client.post("/auth/signup", json={
        "email": "rgn_b@example.com", "password": "password123", "company_name": "Rgn B"
    })

    resp = await http_client.post(f"/sessions/{code}/assessment/regenerate")
    assert resp.status_code == 403
