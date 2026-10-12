"""REST endpoints for session management.

Mirrors [docs/protocol/session.md](../../../docs/protocol/session.md):
- POST /sessions — create (by the interviewer, requires cookie auth)
- GET /sessions — list sessions for the current user (cookie auth)
- GET /sessions/{code} — session config (candidate bootstrap, ?candidate_token=)
- POST /sessions/{code}/end — end the session (cookie auth)
- GET /sessions/{code}/events — replay over REST (cookie auth)
- GET /sessions/{code}/bundle.tar.gz — tar archive of the task (?candidate_token=)
- POST /sessions/{code}/share — create a share_token (cookie auth)
- GET /sessions/{code}/replay — read-only replay (?share_token=)
- GET /sessions/{code}/files — candidate's file tree (cookie auth)
- GET /sessions/{code}/files/content — file contents (cookie auth)

All interviewer endpoints use cookie + Depends(current_user/current_company).
_require_candidate_token was removed. candidate_token is used only for CLI bootstrap.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vibony_backend.assessment.job import regenerate as regenerate_assessment
from vibony_backend.ratelimit import rate_limit
from vibony_backend.assessment.schemas import SessionAssessmentResponse
from vibony_backend.auth import audit
from vibony_backend.auth.deps import current_company, current_user, optional_current_user
from vibony_backend.db.engine import session_scope
from vibony_backend.db.models import Company, Event, Membership
from vibony_backend.db.models import Session as DbSession
from vibony_backend.db.models import SessionAssessment
from vibony_backend.db.models import Task
from vibony_backend.db.models import User
from vibony_backend.schemas import (
    AIConfig,
    EventEnvelope,
    FileTreeEntry,
    FileTreeResponse,
    SessionConfig,
    SessionCreatedResponse,
    SessionCreateRequest,
    SessionListItem,
    SessionListResponse,
    ShareResponse,
)
from vibony_backend.sessions import (
    PROTOCOL_VERSION,
    SESSION_TTL_HOURS,
    SHARE_TOKEN_TTL_DAYS,
    is_expired,
    is_share_token_valid,
)
from vibony_backend.sessions.async_timer import async_timer_registry
from vibony_backend.sessions.broker import broker
from vibony_backend.sessions.bundle import build_bundle_from_files
from vibony_backend.sessions.codes import generate_session_code
from vibony_backend.agent_host.host import agent_host_registry
from vibony_backend.sessions.fs_proxy import web_fs_proxy_registry
from vibony_backend.tasks.store import (
    can_use_task,
    find_task_by_slug,
    resolve_for_session,
)

logger = logging.getLogger(__name__)

TASK_PREFIX_RE = re.compile(r"^task-\d+-")
CODE_GENERATION_MAX_RETRIES = 8

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _derive_task_dir_name(task_id: str) -> str:
    """`task-01-training-loop` → `training-loop`. Without a prefix, the full task_id."""
    return TASK_PREFIX_RE.sub("", task_id) or task_id


async def _resolve_create_task(
    db: AsyncSession,
    *,
    slug: str,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    company_ids: set[uuid.UUID],
) -> Task:
    """Resolve the task a new session runs from its slug.

    Matches a task accessible in the chosen workspace or a system template,
    preferring the workspace's own task over a same-slug template.

    Both "no such task" and "exists but you may not use it" raise the
    same ``400 unknown_task``. A separate 403 on the not-authorized case was an
    existence oracle — a 403 confirmed a private task with that slug lives in the
    workspace (owned by someone else), a 400 said it doesn't. Uniform 400 keeps
    the not-found-hiding contract used everywhere else.
    """
    row = await find_task_by_slug(db, slug, company_id)
    if row is None or not can_use_task(row, user_id=user_id, company_ids=company_ids):
        raise HTTPException(
            status_code=400,
            detail={"reason": "unknown_task", "task_id": slug},
        )
    return row


def _to_session_config(
    row: DbSession, *, ws_token: str = "", candidate_token: str | None = None
) -> SessionConfig:
    return SessionConfig(
        session_code=row.code,
        session_status=row.status,  # type: ignore[arg-type]
        protocol_version=row.protocol_version,
        ws_token=ws_token,
        task_id=row.task_id,
        task_dir_name=_derive_task_dir_name(row.task_id),
        ai_config=row.ai_config,
        bundle_sha256=row.bundle_sha256,
        created_at=row.created_at,
        expires_at=row.expires_at,
        mode=row.mode,  # type: ignore[arg-type]
        time_limit_seconds=row.time_limit_seconds,
        candidate_token=candidate_token,
    )


def _build_candidate_url(candidate_token: str) -> str:
    """Build candidate web URL using VIBONY_FRONTEND_URL (same pattern as invites.py)."""
    frontend_url = os.getenv("VIBONY_FRONTEND_URL", "http://localhost:5173").rstrip("/")
    return f"{frontend_url}/candidate?token={candidate_token}"


async def _load_session(db: AsyncSession, code: str) -> DbSession:
    row = (
        await db.execute(select(DbSession).where(DbSession.code == code))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404, detail={"reason": "not_found", "code": code}
        )
    return row


@router.post("", response_model=SessionCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    request: SessionCreateRequest,
    db: AsyncSession = Depends(session_scope),
    user: User = Depends(current_user),
) -> SessionCreatedResponse:
    """Create a session. Requires cookie auth (interviewer/admin)."""

    # Resolve company from user's memberships.
    memberships = (
        await db.execute(select(Membership).where(Membership.user_id == user.id))
    ).scalars().all()

    if not memberships:
        raise HTTPException(status_code=403, detail={"reason": "no_membership"})

    if request.workspace_id is not None:
        company_id = request.workspace_id
        if not any(m.company_id == company_id for m in memberships):
            raise HTTPException(status_code=403, detail={"reason": "unauthorized"})
    elif len(memberships) == 1:
        company_id = memberships[0].company_id
    else:
        raise HTTPException(
            status_code=422,
            detail={"reason": "workspace_id_required"},
        )

    # Resolve the task (DB) for the chosen workspace + bind the FK.
    task_row = await _resolve_create_task(
        db,
        slug=request.task_id,
        company_id=company_id,
        user_id=user.id,
        company_ids={m.company_id for m in memberships},
    )

    primary_model = request.ai_config.primary_model or os.environ.get("VIBONY_DEFAULT_MODEL")
    if not primary_model:
        raise HTTPException(
            status_code=400,
            detail="ai_config.primary_model is not set in the request and VIBONY_DEFAULT_MODEL is not set in env",
        )
    # Enforce the model allowlist + reject api_base SSRF at create time,
    # so a workspace member can't bind a session to an arbitrary provider/endpoint.
    # Validate fallback_models too — they are inert today (`call_llm` only
    # uses primary_model), but storing an unvalidated provider string is a latent
    # SSRF/allowlist hole the moment graceful degradation is wired to use them.
    from vibony_backend.proxy import ModelNotAllowed, validate_model

    try:
        validate_model(primary_model)
        for fallback in request.ai_config.fallback_models:
            validate_model(fallback)
    except ModelNotAllowed as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    resolved_ai_config = AIConfig(
        primary_model=primary_model,
        fallback_models=request.ai_config.fallback_models,
    )

    now = datetime.now(UTC)
    expires_at = now + timedelta(hours=SESSION_TTL_HOURS)

    for attempt in range(CODE_GENERATION_MAX_RETRIES):
        code = generate_session_code()
        row = DbSession(
            code=code,
            status="pending",
            task_id=task_row.slug,
            task_ref_id=task_row.id,
            protocol_version=PROTOCOL_VERSION,
            ai_config=resolved_ai_config.model_dump(),
            candidate_token=str(uuid.uuid4()),
            company_id=company_id,
            created_by=user.id,
            expires_at=expires_at,
            mode=request.mode,
            time_limit_seconds=request.time_limit_seconds,
        )
        db.add(row)
        try:
            await db.flush()
            break
        except IntegrityError:
            await db.rollback()
            logger.warning("session_code collision attempt=%d code=%s", attempt + 1, code)
            continue
    else:
        raise HTTPException(
            status_code=500,
            detail="Failed to generate a unique session_code",
        )

    await db.commit()
    await db.refresh(row)

    logger.info(
        "session created: code=%s task_id=%s company_id=%s", row.code, row.task_id, row.company_id
    )

    await audit.record(
        company_id=company_id,
        actor_user_id=user.id,
        action=audit.SESSION_CREATED,
        target_type="session",
        target_id=row.code,
        metadata={"task_id": row.task_id, "mode": row.mode},
    )

    config = _to_session_config(row)
    candidate_url = _build_candidate_url(row.candidate_token)
    # candidate_token passed explicitly so SessionCreatedResponse always gets a non-null str.
    base = {k: v for k, v in config.model_dump().items() if k != 'candidate_token'}
    return SessionCreatedResponse(**base, candidate_token=row.candidate_token, candidate_url=candidate_url)


@router.get("", response_model=SessionListResponse)
async def list_sessions(
    db: AsyncSession = Depends(session_scope),
    user: User = Depends(current_user),
    limit: int = Query(default=50, le=50, ge=1),
) -> SessionListResponse:
    """Return sessions for all of the current user's companies."""

    memberships = (
        await db.execute(select(Membership).where(Membership.user_id == user.id))
    ).scalars().all()

    if not memberships:
        return SessionListResponse(sessions=[])

    company_ids = [m.company_id for m in memberships]
    rows = (
        await db.execute(
            select(DbSession)
            .where(DbSession.company_id.in_(company_ids))
            .order_by(DbSession.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()

    return SessionListResponse(
        sessions=[
            SessionListItem(
                session_code=r.code,
                session_status=r.status,  # type: ignore[arg-type]
                task_id=r.task_id,
                task_dir_name=_derive_task_dir_name(r.task_id),
                workspace_id=r.company_id,
                mode=r.mode,  # type: ignore[arg-type]
                time_limit_seconds=r.time_limit_seconds,
                created_at=r.created_at,
                expires_at=r.expires_at,
            )
            for r in rows
        ]
    )


@router.get(
    "/{code}",
    response_model=SessionConfig,
    dependencies=[Depends(rate_limit("session_bootstrap", 120, 60))],
)
async def get_session(
    code: str,
    candidate_token: str | None = Query(default=None),
    db: AsyncSession = Depends(session_scope),
    opt_user: User | None = Depends(optional_current_user),
) -> SessionConfig:
    """Session config. Two auth paths:
    - ?candidate_token=: CLI bootstrap; returns ws_token for WS connection.
    - cookie (interviewer): checks membership, returns config without ws_token.
    """
    # Fail fast before hitting DB if no auth provided at all.
    if candidate_token is None and opt_user is None:
        raise HTTPException(status_code=401, detail={"reason": "unauthorized"})

    row = (
        await db.execute(select(DbSession).where(DbSession.code == code))
    ).scalar_one_or_none()

    if candidate_token is not None:
        # Uniform 401 — an unknown code and a wrong token are indistinguishable
        # to a token holder, so the candidate-token path is not a session-code oracle.
        if row is None or not hmac.compare_digest(candidate_token, row.candidate_token):
            raise HTTPException(status_code=401, detail={"reason": "unauthorized"})
        if row.status == "completed" or is_expired(row.expires_at):
            raise HTTPException(status_code=410, detail={"reason": "completed", "code": code})
        return _to_session_config(row, ws_token=row.candidate_token)

    # Cookie path (opt_user is not None here).
    if row is None:
        raise HTTPException(
            status_code=404, detail={"reason": "not_found", "code": code}
        )
    if row.company_id is not None:
        membership = (
            await db.execute(
                select(Membership).where(
                    Membership.user_id == opt_user.id,  # type: ignore[union-attr]
                    Membership.company_id == row.company_id,
                )
            )
        ).scalar_one_or_none()
        if membership is not None:
            return _to_session_config(row, candidate_token=row.candidate_token)

    raise HTTPException(status_code=403, detail={"reason": "unauthorized"})


@router.post("/{code}/end", response_model=SessionConfig)
async def end_session(
    code: str,
    candidate_token: str | None = Query(default=None),
    db: AsyncSession = Depends(session_scope),
    opt_user: User | None = Depends(optional_current_user),
) -> SessionConfig:
    """End the session (status → completed).

    Authentication: either cookie (interviewer) → completed_reason='interviewer',
    or ?candidate_token= → completed_reason='candidate_quit'.
    Idempotent: a repeat call on an already-completed session is a no-op, 200.

    The persisted/broadcast ``session_end.reason`` is the same
    ``completed_reason`` value (one of the protocol enum
    ``interviewer | timeout | candidate_quit | error | submitted``), not a
    hardcoded ``interviewer``, so the candidate-initiated path is recorded
    faithfully (mirrors ``event_store.store_event``'s ``candidate_quit`` default).
    """
    # Check auth before loading session so unauthenticated requests get 401, not 404.
    if candidate_token is None and opt_user is None:
        raise HTTPException(status_code=401, detail={"reason": "unauthorized"})

    row = (
        await db.execute(select(DbSession).where(DbSession.code == code))
    ).scalar_one_or_none()

    if candidate_token is not None:
        # Candidate-initiated end. Uniform 401 for an unknown code and a wrong
        # token so the path is not a session-code existence oracle.
        if row is None or not hmac.compare_digest(candidate_token, row.candidate_token):
            raise HTTPException(status_code=401, detail={"reason": "unauthorized"})
        completed_reason = "candidate_quit"
    elif opt_user is not None:
        # Interviewer-initiated end — verify membership.
        if row is None:
            raise HTTPException(
                status_code=404, detail={"reason": "not_found", "code": code}
            )
        if row.company_id is None:
            raise HTTPException(status_code=403, detail={"reason": "unauthorized"})
        membership = (
            await db.execute(
                select(Membership).where(
                    Membership.user_id == opt_user.id,
                    Membership.company_id == row.company_id,
                )
            )
        ).scalar_one_or_none()
        if membership is None:
            raise HTTPException(status_code=403, detail={"reason": "unauthorized"})
        completed_reason = "interviewer"
    else:
        raise HTTPException(status_code=401, detail={"reason": "unauthorized"})

    if row.status != "completed":
        # Drain an in-flight agent turn before claiming the
        # session completed, so its trailing events aren't dropped by store_event's
        # active→completed guard and the assessment transcript isn't left mid-turn.
        # Done before the FOR UPDATE row-lock below: quiesce awaits the turn, whose
        # own store_event row-locks the same session — locking first would deadlock.
        host = agent_host_registry.get(code)
        if host is not None:
            await host.quiesce()

        locked = (
            await db.execute(
                select(DbSession).where(DbSession.id == row.id).with_for_update()
            )
        ).scalar_one()

        if locked.status == "completed":
            logger.info("session end no-op (raced with another /end): code=%s", code)
            return _to_session_config(locked)

        was_active = locked.status == "active"
        locked.status = "completed"
        locked.completed_at = datetime.now(UTC)
        locked.completed_reason = completed_reason

        if was_active:
            max_seq: int = (
                await db.execute(
                    select(func.coalesce(func.max(Event.seq), 0)).where(
                        Event.session_id == locked.id
                    )
                )
            ).scalar() or 0
            next_seq = max_seq + 1
            event_id = uuid.uuid4()
            now = datetime.now(UTC)
            payload = {"reason": completed_reason}
            db.add(
                Event(
                    session_id=locked.id,
                    seq=next_seq,
                    id=event_id,
                    type="session_end",
                    client_timestamp=now,
                    payload=payload,
                )
            )

        await db.commit()
        await db.refresh(locked)
        logger.info(
            "session ended: code=%s reason=%s active=%s",
            code,
            completed_reason,
            was_active,
        )

        if was_active:
            envelope = {
                "kind": "event",
                "event": {
                    "id": str(event_id),
                    "seq": next_seq,
                    "session_code": code,
                    "timestamp": now.isoformat(),
                    "type": "session_end",
                    "payload": payload,
                },
            }
            await broker.publish(code, envelope)

        # Tear down the web-mode sandbox immediately. Idempotent:
        # no-op for CLI sessions. The disconnect grace timer remains the
        # fallback when the candidate just closes the tab without ending.
        await agent_host_registry.force_terminate(code)
        # Stop the async countdown so it doesn't keep publishing
        # timer_tick frames after an interviewer ended the session early.
        async_timer_registry.cancel(code)

        # Enqueue the AI assessment job for the just-completed session
        # (both live and async). Best-effort: a failed enqueue must not turn
        # a successful /end into an error. Only for genuinely-active sessions —
        # a pending session that never started has nothing to assess. Idempotent,
        # so an async session already enqueued via end_async_session no-ops here.
        if was_active:
            try:
                from vibony_backend.assessment.job import enqueue

                await enqueue(locked.id, code)
            except Exception as exc:
                logger.warning(
                    "end_session: assessment enqueue failed code=%s: %s", code, exc
                )
    else:
        logger.info("session end no-op (already completed): code=%s", code)

    return _to_session_config(row)


@router.get("/{code}/events", response_model=list[EventEnvelope])
async def get_events(
    code: str,
    since: int = Query(default=0, ge=0),
    share_token: str | None = Query(default=None),
    db: AsyncSession = Depends(session_scope),
    opt_user: User | None = Depends(optional_current_user),
) -> list[EventEnvelope]:
    """Return events with `seq > since`. Auth: cookie (interviewer) or share_token (replay)."""
    if share_token is None and opt_user is None:
        raise HTTPException(status_code=401, detail={"reason": "unauthorized"})

    row = await _load_session(db, code)

    if share_token is not None:
        # Constant-time compare + expiry, both via the shared helper.
        if not is_share_token_valid(row.share_token, row.share_token_expires_at, share_token):
            raise HTTPException(status_code=403, detail={"reason": "unauthorized"})
    elif opt_user is not None:
        if row.company_id is not None:
            membership = (
                await db.execute(
                    select(Membership).where(
                        Membership.user_id == opt_user.id,
                        Membership.company_id == row.company_id,
                    )
                )
            ).scalar_one_or_none()
            if membership is None:
                raise HTTPException(status_code=403, detail={"reason": "unauthorized"})
        elif row.created_by != opt_user.id:
            # A legacy (pre-multi-tenancy) session has no workspace, so
            # there is no membership to gate on — only the interviewer who created
            # it may read its events. A non-creator authenticated caller gets 403
            # (not the old 401: the request *is* authenticated; the failure is
            # authorization). The creator falls through to read access, so legacy
            # rows are no longer dead-access for the person who owns them.
            raise HTTPException(status_code=403, detail={"reason": "unauthorized"})
    else:
        # Unreachable: share_token is None here and the no-auth case 401'd above.
        raise HTTPException(status_code=401, detail={"reason": "unauthorized"})
    events = (
        await db.execute(
            select(Event)
            .where(Event.session_id == row.id, Event.seq > since)
            .order_by(Event.seq)
        )
    ).scalars().all()

    return [
        EventEnvelope(
            id=e.id,
            seq=e.seq,
            session_code=row.code,
            timestamp=e.client_timestamp,
            type=e.type,
            payload=e.payload,
        )
        for e in events
    ]


@router.get("/{code}/bundle.tar.gz")
async def get_bundle(
    code: str,
    candidate_token: str | None = Query(default=None),
    db: AsyncSession = Depends(session_scope),
) -> Response:
    """Download the task bundle. Requires ?candidate_token= (used by the CLI)."""

    if candidate_token is None:
        raise HTTPException(status_code=401, detail={"reason": "unauthorized"})

    # The bundle path is candidate_token-only, the same primary credential as
    # get_session/end_session — uniform 401 for an unknown code and a wrong token so
    # it is not a session-code existence oracle either.
    row = (
        await db.execute(select(DbSession).where(DbSession.code == code))
    ).scalar_one_or_none()
    if row is None or not hmac.compare_digest(candidate_token, row.candidate_token):
        raise HTTPException(status_code=401, detail={"reason": "unauthorized"})

    # Lifecycle gate: no bundle for a completed or expired session.
    if row.status == "completed" or is_expired(row.expires_at):
        raise HTTPException(status_code=410, detail={"reason": "completed", "code": code})

    loaded = await resolve_for_session(db, row)
    if loaded is None:
        raise HTTPException(
            status_code=500,
            detail={"reason": "task_not_found", "task_id": row.task_id},
        )

    tar_bytes, sha256 = build_bundle_from_files(
        loaded.starter_files,
        description_md=loaded.description_md,
        label=row.task_id,
    )

    if row.bundle_sha256 is None:
        row.bundle_sha256 = sha256
        await db.commit()

    return Response(
        content=tar_bytes,
        media_type="application/gzip",
        headers={
            "Content-Disposition": 'attachment; filename="bundle.tar.gz"',
            "X-Bundle-SHA256": sha256,
        },
    )


@router.post("/{code}/share", response_model=ShareResponse)
async def create_share_token(
    code: str,
    db: AsyncSession = Depends(session_scope),
    _auth: tuple = Depends(current_company),
) -> ShareResponse:
    """Create (or return an existing) share_token. Requires cookie auth.

    Only a completed session can be shared (the replay it unlocks is
    completed-only anyway) → no token ever exists for a live/pending session.
    The token is stamped with an expiry at mint; a re-mint on an unexpired token
    is idempotent, but an expired one is rotated + re-stamped.
    """
    row, _ = _auth

    if row.status != "completed":
        raise HTTPException(status_code=409, detail={"reason": "not_completed"})

    now = datetime.now(UTC)
    if row.share_token is None or is_expired(row.share_token_expires_at):
        row.share_token = str(uuid.uuid4())
        row.share_token_expires_at = now + timedelta(days=SHARE_TOKEN_TTL_DAYS)
        await db.commit()
        await db.refresh(row)

    return ShareResponse(
        share_token=row.share_token, expires_at=row.share_token_expires_at
    )


@router.delete("/{code}", status_code=status.HTTP_204_NO_CONTENT)
async def erase_session(
    code: str,
    db: AsyncSession = Depends(session_scope),
    auth: tuple = Depends(current_company),
) -> None:
    """Subject erasure: hard-delete a session and every piece
    of candidate personal data it holds — the transcript event log, the persisted
    LLM message blob (``agent_messages``), and the AI assessment. Events +
    session_assessments cascade at the DB (ON DELETE CASCADE). Requires cookie auth
    (workspace member). Also tears down any live in-process runtime and scrubs the
    session's audit rows so no trace of the subject survives.

    Irreversible. The replay/timeline for this session stop resolving immediately.
    """
    row, _company = auth
    session_id = row.id
    company_id = row.company_id

    # Tear down live runtime so an in-process host/timer/proxy doesn't outlive the row.
    await agent_host_registry.force_terminate(code)
    async_timer_registry.cancel(code)
    web_fs_proxy_registry.unregister(code)

    # Hard-delete via Core so the DB FK cascade removes events + the assessment
    # without loading the (potentially large) transcript into memory.
    await db.execute(delete(DbSession).where(DbSession.id == session_id))
    await db.commit()

    # Scrub the session's own audit trail (session.created targets the code).
    if company_id is not None:
        await audit.purge_for_target(company_id, "session", code)

    logger.info("session erased: code=%s", code)


@router.delete("/{code}/share", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_share_token(
    code: str,
    db: AsyncSession = Depends(session_scope),
    _auth: tuple = Depends(current_company),
) -> None:
    """Revoke the replay share_token. Idempotent: nulls the token + expiry so
    every outstanding replay link stops working. Requires cookie auth (member)."""
    row, _ = _auth
    if row.share_token is not None:
        row.share_token = None
        row.share_token_expires_at = None
        await db.commit()


@router.get(
    "/{code}/replay",
    response_model=SessionConfig,
    dependencies=[Depends(rate_limit("session_bootstrap", 120, 60))],
)
async def get_replay_config(
    code: str,
    share_token: str,
    db: AsyncSession = Depends(session_scope),
) -> SessionConfig:
    """Return SessionConfig for the read-only replay view. Requires share_token."""
    row = await _load_session(db, code)

    # Constant-time compare + expiry. Don't echo the code in error
    # bodies (the token holder already has it; nothing else may probe existence).
    if not is_share_token_valid(row.share_token, row.share_token_expires_at, share_token):
        raise HTTPException(status_code=403, detail={"reason": "unauthorized"})

    # Defensive: a token is only minted for a completed session and sessions never
    # revert, so this is unreachable in the normal flow — kept as belt-and-suspenders.
    if row.status != "completed":
        raise HTTPException(status_code=410, detail={"reason": "not_completed"})

    config = _to_session_config(row)
    config.ws_token = ""
    return config


@router.get("/{code}/files", response_model=FileTreeResponse)
async def list_files(
    code: str,
    _auth: tuple = Depends(current_company),
) -> FileTreeResponse:
    """Return the candidate's file tree. Requires cookie auth."""
    row, _ = _auth

    # Route via WebFsProxy → sandbox.
    web_proxy = web_fs_proxy_registry.get(code)
    if web_proxy is None:
        raise HTTPException(
            status_code=503,
            detail={"reason": "sandbox_unavailable", "message": "Web sandbox is not ready"},
        )
    response = await web_proxy.request("tree")
    if "error" in response:
        raise HTTPException(
            status_code=500,
            detail={"reason": "sandbox_error", "message": response["error"]},
        )
    raw_entries = response.get("entries") or []
    entries = [
        FileTreeEntry(path=e["path"], kind=e["kind"], size=int(e.get("size", 0)))
        for e in raw_entries
        if isinstance(e, dict) and e.get("kind") in ("file", "dir")
    ]
    return FileTreeResponse(entries=entries)


@router.get("/{code}/files/content")
async def get_file_content(
    code: str,
    path: str,
    _auth: tuple = Depends(current_company),
) -> Response:
    """Return the candidate's file contents. Requires cookie auth."""
    from pathlib import PurePosixPath

    if not path or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
        raise HTTPException(
            status_code=400,
            detail={"reason": "invalid_path", "message": "path must be a relative path within the workdir"},
        )

    row, _ = _auth

    # Route via WebFsProxy → sandbox.
    web_proxy = web_fs_proxy_registry.get(code)
    if web_proxy is None:
        raise HTTPException(
            status_code=503,
            detail={"reason": "sandbox_unavailable", "message": "Web sandbox is not ready"},
        )
    response = await web_proxy.request("read", path=path)
    if "error" in response:
        error_msg = response["error"]
        status = 404 if "not found" in str(error_msg).lower() else 500
        reason = "not_found" if status == 404 else "sandbox_error"
        raise HTTPException(status_code=status, detail={"reason": reason, "message": error_msg})
    content = response.get("content", "")
    return Response(content=content, media_type="text/plain; charset=utf-8")


# -------- POST /sessions/{code}/sandbox/reset --------


@router.post("/{code}/sandbox/reset", status_code=status.HTTP_200_OK)
async def reset_sandbox(
    code: str,
    _auth: tuple = Depends(current_company),
) -> dict:
    """Reset the web candidate's sandbox.

    Destroys the current container and recreates it with the task baseline.
    Conversation history is preserved. Active sessions only.
    """
    row, _ = _auth

    if row.status == "completed":
        # Completed-state rejection is 410 Gone, consistent with the
        # bundle / get_session / replay gates (was a one-off 400 here).
        raise HTTPException(
            status_code=410,
            detail={"reason": "completed", "message": "Cannot reset sandbox of a completed session"},
        )

    host = agent_host_registry.get(code)
    if host is None:
        raise HTTPException(
            status_code=404,
            detail={"reason": "sandbox_not_found", "message": "No active sandbox for this session (candidate not connected)"},
        )

    await host.reset_sandbox()
    return {"status": "ok"}


# -------- AI assessment --------


@router.get("/{code}/assessment", response_model=SessionAssessmentResponse)
async def get_assessment(
    code: str,
    db: AsyncSession = Depends(session_scope),
    _auth: tuple = Depends(current_company),
) -> SessionAssessmentResponse:
    """Return the session's AI assessment.

    Interviewer membership only: ``current_company`` requires a cookie that
    is a member of the session's workspace — a ``candidate_token`` / ``share_token``
    is **not** accepted (the report contains the task traps, must never reach the
    candidate or a public replay link). The dashboard polls this while the status
    is ``pending``/``running``. No row yet → synthesized ``not_started`` status.
    """
    row, _ = _auth
    assessment = (
        await db.execute(
            select(SessionAssessment).where(SessionAssessment.session_id == row.id)
        )
    ).scalar_one_or_none()
    if assessment is None:
        return SessionAssessmentResponse(status="not_started")
    return SessionAssessmentResponse.model_validate(assessment)


@router.post(
    "/{code}/assessment/regenerate",
    response_model=SessionAssessmentResponse,
    dependencies=[Depends(rate_limit("assessment_regenerate", 10, 60))],
)
async def regenerate_assessment_endpoint(
    code: str,
    db: AsyncSession = Depends(session_scope),
    _auth: tuple = Depends(current_company),
) -> SessionAssessmentResponse:
    """Reset the assessment to ``pending`` and re-run it. Interviewer only.

    Backs the dashboard "Regenerate" action (e.g. after tuning the task spec).
    ``regenerate`` commits the reset in its own session and spawns the job; we then
    re-read the (now ``pending``) row — READ COMMITTED makes the committed reset
    visible to this request's session.

    Only a completed session can be (re)assessed — its transcript is final.
    Regenerating a live/never-started session would spawn an LLM job on an
    incomplete transcript (cost + a meaningless report) → 409 ``not_completed``
    (mirrors ``POST /share``).

    A *completed* session that never went active (created then ended with no
    candidate ever connecting → empty transcript) also has nothing to assess. The
    auto-enqueue path gates on ``was_active``; the original status-only gate
    let this case through and spent an LLM call on an empty report. Event
    existence is a faithful ``was_active`` proxy — ``store_event`` claims
    pending→active on the first event, so a never-active session has none → 409
    ``empty_session``.
    """
    row, _ = _auth
    if row.status != "completed":
        raise HTTPException(status_code=409, detail={"reason": "not_completed"})
    has_events = (
        await db.execute(select(Event.id).where(Event.session_id == row.id).limit(1))
    ).first() is not None
    if not has_events:
        raise HTTPException(status_code=409, detail={"reason": "empty_session"})
    await regenerate_assessment(row.id, code)
    assessment = (
        await db.execute(
            select(SessionAssessment).where(SessionAssessment.session_id == row.id)
        )
    ).scalar_one_or_none()
    if assessment is None:
        # regenerate always upserts a pending row, so this is defensive only.
        return SessionAssessmentResponse(status="pending")
    return SessionAssessmentResponse.model_validate(assessment)


# -------- GET /sessions/by-candidate-token/{token} --------


class _CandidateTokenConfig(SessionConfig):
    """SessionConfig returned by the public token-bootstrap endpoint.

    Same shape as SessionConfig but always includes `ws_token` equal to the
    candidate_token (so the browser can authenticate future WS connections).
    Added for the web candidate runtime bootstrap.
    """


@router.get(
    "/by-candidate-token/{token}",
    response_model=SessionConfig,
    dependencies=[Depends(rate_limit("session_bootstrap", 120, 60))],
)
async def get_session_by_candidate_token(
    token: str,
    db: AsyncSession = Depends(session_scope),
) -> SessionConfig:
    """Public endpoint: resolve session by candidate_token.

    Used by the web candidate SPA bootstrap. No cookie auth required.
    Returns 401 if token is invalid, 410 if session is completed.
    """
    # candidate_token is the lookup key (no code), so this indexed
    # equality can't be a constant-time compare — there's no row to load first.
    # 122-bit random uuid4; B-tree timing is not a realistic recovery vector.
    row = (
        await db.execute(
            select(DbSession).where(DbSession.candidate_token == token)
        )
    ).scalar_one_or_none()

    if row is None:
        raise HTTPException(status_code=401, detail={"reason": "unauthorized"})

    if row.status == "completed" or is_expired(row.expires_at):
        raise HTTPException(
            status_code=410, detail={"reason": "completed", "code": row.code}
        )

    return _to_session_config(row, ws_token=row.candidate_token)
