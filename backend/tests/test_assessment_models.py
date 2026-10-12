"""Storage tests: session_assessments table + SessionAssessment model.

Covers:
- Alembic head is 0007 (the migration is in the chain).
- A row round-trips with findings JSONB (nested list preserved).
- A pending row gets the server defaults (findings == [], everything else NULL).
- UNIQUE(session_id) rejects a second assessment for the same session (1:1).
- DB-level ON DELETE CASCADE: deleting the session removes its assessment.
- SessionAssessmentResponse parses a dict into typed findings (pure, no DB).

Mixed sync + async file → per-test `_session_loop` mark (see backend/CLAUDE.md
"Test suite traps"), no module-level pytestmark.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vibony_backend.assessment.schemas import SessionAssessmentResponse
from vibony_backend.db.models import Session as DbSession, SessionAssessment

BACKEND_ROOT = Path(__file__).resolve().parent.parent

_session_loop = pytest.mark.asyncio(loop_scope="session")


def _make_session(code: str = "ASMT-0001") -> DbSession:
    now = datetime.now(UTC)
    return DbSession(
        code=code,
        status="completed",
        task_id="task-01-training-loop",
        protocol_version=7,
        ai_config={"primary_model": "deepseek/deepseek-chat"},
        candidate_token=str(uuid.uuid4()),
        expires_at=now + timedelta(hours=8),
    )


def test_alembic_single_head():
    """The migration graph must be linear — exactly one head, no branching.

    (Pinning a specific revision number was brittle: it broke on every new
    migration. Asserting a single head catches the real bug — two parallel
    down_revisions creating divergent heads — without a per-migration edit.)"""
    cfg = Config()
    cfg.set_main_option(
        "script_location",
        str(BACKEND_ROOT / "vibony_backend" / "db" / "migrations"),
    )
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1


@_session_loop
async def test_assessment_round_trips_findings(db_session: AsyncSession):
    s = _make_session("ASMT-RT01")
    db_session.add(s)
    await db_session.flush()

    a = SessionAssessment(
        session_id=s.id,
        status="ready",
        model="deepseek/deepseek-chat",
        summary_md="## What went well\nUsed checkpoints.",
        findings=[
            {
                "category": "strength",
                "title": "Saved checkpoints",
                "detail": "Persisted state each epoch.",
                "severity": None,
                "event_seq": 42,
            },
            {
                "category": "gap",
                "title": "Missed best-weights restore",
                "detail": "Never reloaded the best checkpoint.",
                "severity": "blocking",
                "event_seq": None,
            },
        ],
    )
    db_session.add(a)
    await db_session.flush()
    db_session.expire(a)

    fetched = (
        await db_session.execute(
            select(SessionAssessment).where(SessionAssessment.session_id == s.id)
        )
    ).scalar_one()

    assert fetched.status == "ready"
    assert fetched.model == "deepseek/deepseek-chat"
    assert fetched.summary_md.startswith("## What went well")
    assert len(fetched.findings) == 2
    assert fetched.findings[0]["category"] == "strength"
    assert fetched.findings[0]["event_seq"] == 42
    assert fetched.findings[1]["severity"] == "blocking"
    assert fetched.created_at is not None
    assert fetched.updated_at is not None


@_session_loop
async def test_assessment_pending_defaults(db_session: AsyncSession):
    """A minimal pending row: findings defaults to [] (server_default), rest NULL."""
    s = _make_session("ASMT-PND1")
    db_session.add(s)
    await db_session.flush()

    a = SessionAssessment(session_id=s.id, status="pending")
    db_session.add(a)
    await db_session.flush()
    await db_session.refresh(a)

    assert a.findings == []
    assert a.summary_md is None
    assert a.model is None
    assert a.error is None


@_session_loop
async def test_assessment_session_id_unique(db_session: AsyncSession):
    """UNIQUE(session_id) → a second assessment for the same session fails (1:1)."""
    s = _make_session("ASMT-UNQ1")
    db_session.add(s)
    await db_session.flush()

    db_session.add(SessionAssessment(session_id=s.id, status="pending"))
    await db_session.flush()

    db_session.add(SessionAssessment(session_id=s.id, status="running"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@_session_loop
async def test_assessment_cascade_delete(db_session: AsyncSession):
    """Deleting the session cascades to its assessment at the DB level."""
    s = _make_session("ASMT-CSC1")
    db_session.add(s)
    await db_session.flush()

    db_session.add(SessionAssessment(session_id=s.id, status="ready"))
    await db_session.flush()

    # Raw SQL delete to exercise FK ON DELETE CASCADE, not ORM cascade.
    await db_session.execute(sa.delete(DbSession).where(DbSession.id == s.id))
    await db_session.flush()

    remaining = (
        await db_session.execute(
            select(SessionAssessment).where(SessionAssessment.session_id == s.id)
        )
    ).scalars().all()
    assert remaining == []


def test_response_schema_parses_findings():
    """SessionAssessmentResponse coerces dict findings into typed AssessmentFinding."""
    resp = SessionAssessmentResponse.model_validate(
        {
            "status": "ready",
            "summary_md": "## Observed reasoning patterns\n...",
            "findings": [
                {
                    "category": "gap",
                    "title": "No verification run",
                    "detail": "Never executed the training loop.",
                    "severity": "degrading",
                    "event_seq": 3,
                }
            ],
            "model": "deepseek/deepseek-chat",
            "error": None,
        }
    )
    assert resp.status == "ready"
    assert len(resp.findings) == 1
    assert resp.findings[0].category == "gap"
    assert resp.findings[0].severity == "degrading"
    assert resp.findings[0].event_seq == 3


def test_response_schema_not_started_defaults():
    """A 'not_started' response defaults to empty findings + null fields."""
    resp = SessionAssessmentResponse(status="not_started")
    assert resp.findings == []
    assert resp.summary_md is None
    assert resp.model is None
