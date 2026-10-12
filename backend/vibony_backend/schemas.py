"""Pydantic schemas for the backend.

ChatRequest/ChatResponse — for the AI proxy.
AIConfig, Session* schemas — for the REST sessions API; they mirror
[docs/protocol/session.md](../../docs/protocol/session.md) and
[docs/protocol/events.md](../../docs/protocol/events.md).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


def _require_non_blank(v: str) -> str:
    """Strip and reject a whitespace-only value.

    ``min_length=1`` lets a single space through; without this the value is then
    stored verbatim (signup) or stripped to empty (create-workspace) — an
    inconsistent, blank-named company. Strip + re-check + store the stripped form
    so every name-bearing request behaves the same. A blank value → 422."""
    stripped = v.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


# --- AI proxy ---


class ChatRequest(BaseModel):
    task_id: str = Field(..., max_length=256, description="Task ID in tasks/, source of weakness.yaml")
    # No item caps on messages/tools: this schema is REUSED on the internal agent
    # hot path (agent_host/loop.py rebuilds ChatRequest with the full accumulated
    # blob each iteration), where a cap would wedge a long session. The external
    # /v1/chat boundary is bounded by BodySizeLimitMiddleware instead.
    messages: list[dict] = Field(..., description="OpenAI-format messages (no system — the backend injects it)")
    tools: list[dict] | None = Field(default=None, description="OpenAI-format tool definitions")
    model: str | None = Field(default=None, description="Model override; defaults to env VIBONY_DEFAULT_MODEL")
    system_prompt: str | None = Field(
        default=None,
        description=(
            "Pre-rendered weakness system prompt. When set, `call_llm` "
            "injects it verbatim instead of resolving `render_weakness(task_id)` "
            "from the filesystem — the web AgentHost renders it once from the DB "
            "task at start. Omitted by the legacy /v1/chat path."
        ),
    )
    session_code: str | None = Field(
        default=None,
        description=(
            "Session code (XXXX-XXXX). When set, the backend checks that the "
            "session exists and `status == 'active'` — otherwise 410 Gone. Without "
            "this, the CLI keeps driving the AI after 'End session' in the "
            "dashboard even if the WS shutdown signal never arrived. In local mode "
            "(`vibony chat`) the field is omitted."
        ),
    )
    timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Per-call LLM timeout. When set, `call_llm` uses it instead of "
            "the absolute `VIBONY_LLM_CALL_TIMEOUT_SECONDS` ceiling — the agent loop "
            "caps it at the remaining async-session budget so a hung call can't run "
            "past the candidate's deadline."
        ),
    )


class ChatResponse(BaseModel):
    content: str | None = Field(default=None, description="The model's text response")
    tool_calls: list[dict] | None = Field(default=None, description="Tool calls, if the model made any")
    finish_reason: str | None = Field(default=None, description="stop | tool_calls | length | content_filter")
    usage: dict | None = Field(default=None, description="{prompt_tokens, completion_tokens, total_tokens}")
    model: str = Field(..., description="Which model actually responded")


# --- Sessions REST ---


SessionStatus = Literal["pending", "active", "completed"]


class AIConfig(BaseModel):
    primary_model: str
    fallback_models: list[str] = Field(default_factory=list)


class AIConfigInput(BaseModel):
    """Input format of `ai_config` in SessionCreateRequest. All fields optional:
    `primary_model` defaults to env `VIBONY_DEFAULT_MODEL`."""

    primary_model: str | None = None
    fallback_models: list[str] = Field(default_factory=list)


SessionMode = Literal["live", "async"]

# Async time-limit bounds: 5 min – 4 h. Server-side deadline authority.
MIN_TIME_LIMIT_SECONDS = 5 * 60
MAX_TIME_LIMIT_SECONDS = 4 * 60 * 60


class SessionCreateRequest(BaseModel):
    task_id: str
    ai_config: AIConfigInput = Field(default_factory=AIConfigInput)
    workspace_id: uuid.UUID | None = None
    mode: SessionMode = "live"
    time_limit_seconds: int | None = Field(
        default=None,
        description=(
            "Async deadline window in seconds. Required for mode='async' "
            f"({MIN_TIME_LIMIT_SECONDS}–{MAX_TIME_LIMIT_SECONDS}); forbidden for "
            "mode='live'."
        ),
    )

    @model_validator(mode="after")
    def _validate_time_limit(self) -> SessionCreateRequest:
        if self.mode == "async":
            if self.time_limit_seconds is None:
                raise ValueError("time_limit_seconds is required for mode='async'")
            if not (
                MIN_TIME_LIMIT_SECONDS
                <= self.time_limit_seconds
                <= MAX_TIME_LIMIT_SECONDS
            ):
                raise ValueError(
                    "time_limit_seconds must be between "
                    f"{MIN_TIME_LIMIT_SECONDS} and {MAX_TIME_LIMIT_SECONDS}"
                )
        elif self.time_limit_seconds is not None:
            raise ValueError("time_limit_seconds is only valid for mode='async'")
        return self


class SessionConfig(BaseModel):
    """What GET /sessions/{code} returns to the client."""

    model_config = ConfigDict(from_attributes=True)

    session_code: str
    session_status: SessionStatus
    protocol_version: int
    ws_token: str = Field(
        ..., description="Currently == session_code; will become a JWT later"
    )
    task_id: str
    task_dir_name: str = Field(
        ..., description="Candidate's local directory name, derived from task_id"
    )
    ai_config: AIConfig
    bundle_sha256: str | None = Field(
        default=None, description="Populated on the first bundle build"
    )
    created_at: datetime
    expires_at: datetime
    mode: SessionMode = "live"
    time_limit_seconds: int | None = None
    candidate_token: str | None = Field(
        default=None,
        description="Populated only for authenticated interviewer calls; None for candidate-auth path.",
    )

    @field_validator("ai_config", mode="before")
    @classmethod
    def _coerce_legacy_ai_config(cls, v: object) -> object:
        """A malformed legacy ``ai_config`` JSONB must not 500 the read path.

        ``AIConfig`` requires a non-empty ``primary_model`` and a ``list[str]``
        ``fallback_models``; a hand-edited row violating either would
        raise on serialization of every session-config response. Coerce a dict with
        a missing/blank ``primary_model`` **or** non-string ``fallback_models``
        items into a safe shape so the endpoint degrades instead of 500ing.

        Follow-up to the first pass, which only guarded ``primary_model``:
        a valid ``primary_model`` with e.g. ``fallback_models=[123]`` still 500ed
        because the unchanged dict reached ``AIConfig``'s ``list[str]`` validator.
        """
        if isinstance(v, dict):
            model = v.get("primary_model")
            fallbacks = v.get("fallback_models")
            model_ok = isinstance(model, str) and bool(model.strip())
            fallbacks_ok = fallbacks is None or (
                isinstance(fallbacks, list) and all(isinstance(s, str) for s in fallbacks)
            )
            if not model_ok or not fallbacks_ok:
                safe_fallbacks = (
                    [s for s in fallbacks if isinstance(s, str)]
                    if isinstance(fallbacks, list)
                    else []
                )
                return {
                    "primary_model": model if model_ok else "",
                    "fallback_models": safe_fallbacks,
                }
        return v


class SessionCreatedResponse(SessionConfig):
    """Extends SessionConfig with candidate_token (issued only to the creator)."""

    candidate_token: str
    candidate_url: str | None = None


class SessionListItem(BaseModel):
    """One row in GET /sessions response."""

    model_config = ConfigDict(from_attributes=True)

    session_code: str
    session_status: SessionStatus
    task_id: str
    task_dir_name: str
    workspace_id: uuid.UUID | None
    mode: SessionMode = "live"
    time_limit_seconds: int | None = None
    created_at: datetime
    expires_at: datetime


class SessionListResponse(BaseModel):
    sessions: list[SessionListItem]


class EventEnvelope(BaseModel):
    """Matches the Envelope from docs/protocol/events.md."""

    id: uuid.UUID
    seq: int
    session_code: str
    timestamp: datetime
    type: str
    payload: dict


# --- Share / Replay ---


class ShareResponse(BaseModel):
    """Returned by POST /sessions/{code}/share."""

    share_token: str
    # When the replay link stops working. Stamped at mint.
    expires_at: datetime | None = None


# --- Files REST ---


class FileTreeEntry(BaseModel):
    path: str
    kind: Literal["file", "dir"] = "file"
    size: int = 0


class FileTreeResponse(BaseModel):
    entries: list[FileTreeEntry]


# --- Auth ---


# Upper bound on password length: argon2 hashing cost grows
# with input, so an unbounded password is a CPU-DoS primitive. 128 is the common
# OWASP cap and well above any real passphrase.
MAX_PASSWORD_LENGTH = 128


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=MAX_PASSWORD_LENGTH)
    company_name: str = Field(..., min_length=1, max_length=200)

    _strip_company_name = field_validator("company_name")(_require_non_blank)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., max_length=MAX_PASSWORD_LENGTH)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    created_at: datetime
    # Derived from User.email_verified_at via from_attributes (model property).
    email_verified: bool = False


class VerifyEmailRequest(BaseModel):
    token: str = Field(..., max_length=4096)


class CompanyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str


class SignupResponse(BaseModel):
    user: UserResponse
    company: CompanyResponse


class MembershipResponse(BaseModel):
    workspace_id: uuid.UUID
    company_name: str
    role: str
    # Billing-plan stub surfaced to the dashboard.
    plan: str = "trial"
    trial_ends_at: datetime | None = None


class MeResponse(BaseModel):
    user: UserResponse
    memberships: list[MembershipResponse]


class WorkspaceCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)

    _strip_name = field_validator("name")(_require_non_blank)


class AuthConfigResponse(BaseModel):
    mode: Literal["password", "oidc"]
    oidc_login_url: str | None


# --- Invites + Team ---

InviteStatus = Literal["pending", "accepted", "expired", "revoked"]


class InviteCreateRequest(BaseModel):
    role: Literal["admin", "interviewer"]
    # EmailStr (was bare str) so an invite email is validated + length-bounded
    # before it lands in audit_log.metadata.
    email: EmailStr | None = None


class InviteSignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=MAX_PASSWORD_LENGTH)


class InviteResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    token: str
    email: str | None
    role: str
    invited_by: uuid.UUID
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None
    accepted_by: uuid.UUID | None
    status: InviteStatus
    accept_url: str | None = None


class InvitePreview(BaseModel):
    company_name: str
    inviter_email: str
    invited_email: str | None  # the email the invite was created for, if any
    role: str
    expires_at: datetime
    status: InviteStatus


class MemberListItem(BaseModel):
    user_id: uuid.UUID
    email: str
    role: str
    joined_at: datetime


class MemberListResponse(BaseModel):
    members: list[MemberListItem]


class MemberRoleUpdateRequest(BaseModel):
    role: Literal["admin", "interviewer"]


# --- Audit log ---


class AuditLogItem(BaseModel):
    id: uuid.UUID
    action: str
    actor_email: str | None
    target_type: str | None
    target_id: str | None
    # The target's email resolved at read time (not stored in
    # metadata). None when the target was erased or has no email.
    target_email: str | None = None
    metadata: dict
    created_at: datetime


class AuditLogResponse(BaseModel):
    entries: list[AuditLogItem]
