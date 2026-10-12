"""Pydantic schemas for the AI session assessment.

`AssessmentFinding` is the structured, deep-linkable unit. It doubles as
the LLM structured-output contract (validated) and as the wire shape
inside `SessionAssessmentResponse`, the body of GET /sessions/{code}/assessment.
The prose lives in `summary_md` (3 H2 sections, rendered by the dashboard
Markdown component); the structured part lives in `findings`.

No behaviour wired yet — these are types only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# DB-stored job states (session_assessments.status). The REST layer also
# synthesizes "not_started" when no row exists yet — hence it appears in the
# response Literal below but never in the table.
AssessmentStatus = Literal["pending", "running", "ready", "failed"]

FindingCategory = Literal["strength", "gap", "pattern"]
# Mirrors the spec.yaml severities for expected_ai_pitfalls.
FindingSeverity = Literal["blocking", "degrading", "info"]


class AssessmentFinding(BaseModel):
    """One structured observation. `category` groups it under the summary_md
    sections; `event_seq` cites the event log."""

    category: FindingCategory
    title: str = Field(..., description="Short headline")
    detail: str = Field(..., description="Explanation")
    severity: FindingSeverity | None = None
    event_seq: int | None = Field(
        default=None,
        description="Citation into the session event log.",
    )


class AssessmentPayload(BaseModel):
    """The LLM structured-output contract.

    The model returns exactly this JSON object; it is validated against this
    schema (one retry on failure). `summary_md` is the 3-section prose blob;
    `findings` is the structured list. Stored split across the
    `session_assessments` row (`summary_md` Text + `findings` JSONB)."""

    summary_md: str
    findings: list[AssessmentFinding] = Field(default_factory=list)


class SessionAssessmentResponse(BaseModel):
    """Body of GET /sessions/{code}/assessment.

    Built from the `SessionAssessment` row via `from_attributes`; status
    'not_started' is synthesized by the endpoint when no row exists.
    `summary_md`/`findings`/`model` are populated only when status == 'ready';
    `error` only when status == 'failed'.
    """

    model_config = ConfigDict(from_attributes=True)

    status: Literal["not_started", "pending", "running", "ready", "failed"]
    summary_md: str | None = None
    findings: list[AssessmentFinding] = Field(default_factory=list)
    model: str | None = None
    error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
