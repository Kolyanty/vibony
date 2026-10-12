"""Vibony backend FastAPI app.

One endpoint `POST /v1/chat` (AI proxy). WebSocket endpoints for CLI and
dashboard plus REST for session management are added later.
"""

from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from dotenv import load_dotenv

# Load `.env` BEFORE importing proxy: `DEFAULT_MODEL` and litellm provider
# keys are read at module-load time.
load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from vibony_backend.auth.invites import router as invites_router  # noqa: E402
from vibony_backend.auth.oidc import router as oidc_router  # noqa: E402
from vibony_backend.auth.routes import router as auth_router  # noqa: E402
from vibony_backend.auth.workspaces import router as workspaces_router  # noqa: E402
from vibony_backend.db.engine import session_scope  # noqa: E402
from vibony_backend.db.models import Session as DbSession  # noqa: E402
from vibony_backend.llm_usage import session_usage, UsageLimitExceeded
from vibony_backend.proxy import ModelNotAllowed, call_llm  # noqa: E402
from vibony_backend.ratelimit import (  # noqa: E402
    BodySizeLimitMiddleware,
    SecurityHeadersMiddleware,
)
from vibony_backend.schemas import ChatRequest, ChatResponse  # noqa: E402
from vibony_backend.sessions import is_expired  # noqa: E402
from vibony_backend.sessions.rest import router as sessions_router  # noqa: E402
from vibony_backend.sessions.ws import router as sessions_ws_router  # noqa: E402
from vibony_backend.tasks.rest import router as tasks_router  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Fail fast on a missing/weak signing secret — it signs session
    # cookies + OIDC state + email-verify tokens.
    from vibony_backend.auth.cookies import validate_session_secret

    validate_session_secret()

    # In production fail closed on default DB creds / missing
    # DB TLS / non-https frontend URL / non-Secure cookie. No-op outside production.
    from vibony_backend.deploy import assert_production_config

    assert_production_config()

    # In OIDC mode the callback/redirect_uri must come from a fixed base URL,
    # never the (spoofable) Host header. Fail fast if it's unset.
    if os.getenv("VIBONY_AUTH_MODE", "password").lower() == "oidc" and not os.getenv(
        "VIBONY_BASE_URL", ""
    ).strip():
        raise RuntimeError(
            "VIBONY_BASE_URL must be set when VIBONY_AUTH_MODE=oidc "
            "(the OIDC redirect_uri is derived from it, never from the Host header)."
        )

    # Reap Docker sandbox orphans from previous backend runs.
    if os.getenv("VIBONY_SANDBOX_PROVIDER") == "docker":
        try:
            from vibony_backend.sandbox.config import SANDBOX_RUNTIME
            from vibony_backend.sandbox.docker_provider import DockerSandboxProvider
            from vibony_backend.sandbox.reaper import reap_orphans

            logger.info(
                "Startup: sandbox runtime=%s",
                SANDBOX_RUNTIME or "runc (docker default)",
            )
            provider = DockerSandboxProvider()
            count = await reap_orphans(provider)
            if count:
                logger.info("Startup: reaped %d orphaned sandbox container(s)", count)
        except Exception as exc:
            logger.warning("Startup: sandbox reaper failed: %s", exc)

    # Re-arm async countdown timers lost on restart. Independent of the
    # sandbox provider, so it runs on every startup — a deadline that lapses
    # while no candidate is connected still drives the session to completion.
    try:
        from vibony_backend.sessions.async_timer import recover_async_timers

        await recover_async_timers()
    except Exception as exc:
        logger.warning("Startup: async timer recovery failed: %s", exc)

    # Re-run assessment jobs stranded in pending/running by a restart.
    # In-process task, so a restart loses it; same recovery pattern as the timers.
    try:
        from vibony_backend.assessment.job import recover_pending_assessments

        await recover_pending_assessments()
    except Exception as exc:
        logger.warning("Startup: assessment job recovery failed: %s", exc)

    # Prune audit rows past the retention window. Opt-in via
    # VIBONY_AUDIT_RETENTION_DAYS (unset/0 = keep forever). Best-effort.
    try:
        from vibony_backend.auth import audit

        retention_days = int(os.getenv("VIBONY_AUDIT_RETENTION_DAYS", "0"))
        pruned = await audit.prune_older_than(retention_days)
        if pruned:
            logger.info("Startup: pruned %d audit row(s) past retention", pruned)
    except Exception as exc:
        logger.warning("Startup: audit retention prune failed: %s", exc)

    # Seed/refresh the reference tasks as templates owned by the system workspace
    # and backfill older sessions' task_ref_id. Idempotent (upsert), so
    # it re-syncs the templates from the repo files on every deploy.
    try:
        from vibony_backend.tasks.seed import run_template_seed

        await run_template_seed()
    except Exception as exc:
        logger.warning("Startup: template task seed failed: %s", exc)
    yield


# The interactive API docs are off unless VIBONY_ENABLE_DOCS is set, so a prod
# deploy never serves the schema (/docs, /redoc, /openapi.json) to the internet.
from vibony_backend.deploy import docs_enabled  # noqa: E402

_docs_on = docs_enabled()
app = FastAPI(
    title="Vibony backend",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs_on else None,
    redoc_url="/redoc" if _docs_on else None,
    openapi_url="/openapi.json" if _docs_on else None,
)

# Reject oversized request bodies before they are buffered/parsed.
_MAX_BODY_BYTES = int(os.getenv("VIBONY_MAX_BODY_BYTES", str(5 * 1024 * 1024)))
app.add_middleware(BodySizeLimitMiddleware, max_bytes=_MAX_BODY_BYTES)
# Baseline security response headers.
app.add_middleware(SecurityHeadersMiddleware)

# Host allowlist: reject requests with an unexpected Host header so
# nothing downstream can be tricked into trusting a spoofed Host. Unset → no
# restriction (dev/test); set VIBONY_ALLOWED_HOSTS=app.example.com,... in prod.
_allowed_hosts = [
    h.strip() for h in os.getenv("VIBONY_ALLOWED_HOSTS", "").split(",") if h.strip()
]
if _allowed_hosts:
    from starlette.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts)

app.include_router(auth_router)
app.include_router(oidc_router)
app.include_router(invites_router)
app.include_router(workspaces_router)
app.include_router(sessions_router)
app.include_router(sessions_ws_router)
app.include_router(tasks_router)


@app.get("/health")
async def health() -> dict:
    """Liveness — the process is up. No dependency checks (cheap, always 200)."""
    return {"status": "ok"}


@app.get("/ready")
async def ready(db: AsyncSession = Depends(session_scope)) -> dict:
    """Readiness: the process can reach the DB. 503 if the ping fails so an
    orchestrator/load balancer holds traffic until the backend is actually usable."""
    from sqlalchemy import text

    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — surface as not-ready, not a 500
        logger.warning("readiness check failed: %s", exc)
        raise HTTPException(status_code=503, detail={"status": "not_ready"}) from exc
    return {"status": "ready"}


@app.post("/v1/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    db: AsyncSession = Depends(session_scope),
    authorization: str | None = Header(default=None),
) -> ChatResponse:
    """AI proxy. In session mode requires Bearer and `status == 'active'`.

    If `request.session_code` is set:
    - require `Authorization: Bearer <candidate_token>` (token == candidate_token);
      without it — 401, with a wrong one — 403. Symmetric with WS authentication;
    - if the session is not found or is `pending`/`completed` — 410 Gone. This is a
      server-side gate: even if the CLI missed the WS shutdown signal and keeps
      sending requests, the backend rejects them.

    In local mode (`vibony chat`) `session_code` is omitted — neither auth nor
    status checks are active, behavior is unchanged.
    """
    if request.session_code is None:
        # The session-less path is an unauthenticated open LLM relay (legacy /
        # dev only). Disabled by default — gate behind an explicit flag so a
        # production deploy never exposes a free, anonymous LLM endpoint.
        if os.getenv("VIBONY_ALLOW_UNAUTH_CHAT", "").strip().lower() not in (
            "1",
            "true",
            "yes",
            "on",
        ):
            raise HTTPException(status_code=404, detail={"reason": "not_found"})

    if request.session_code is not None:
        row = (
            await db.execute(
                select(DbSession).where(DbSession.code == request.session_code)
            )
        ).scalar_one_or_none()
        if row is None:
            # 410, not 404: for the CLI "no session" and "session completed" are
            # the same terminal outcome. The reason is identical ("completed"),
            # without `code`/`status` — so the client can't tell "no such session"
            # from "exists but completed" by the response shape (protects against
            # code enumeration; auth-before-status above works only for existing
            # sessions, and for a None-session there is no token).
            raise HTTPException(status_code=410, detail={"reason": "completed"})

        # Auth BEFORE the status check: otherwise the status code could be used
        # to guess a session's existence without knowing the token.
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail={"reason": "unauthorized"})
        token = authorization.split(None, 1)[1].strip()
        # Constant-time compare so the candidate_token gate can't be probed
        # by timing (symmetric with the REST/WS gates' hmac.compare_digest).
        if not hmac.compare_digest(token, row.candidate_token):
            raise HTTPException(status_code=403, detail={"reason": "unauthorized"})

        if row.status != "active" or is_expired(row.expires_at):
            raise HTTPException(
                status_code=410,
                detail={
                    "reason": "completed",
                    "code": request.session_code,
                    "status": row.status,
                },
            )

        # Do not trust client-supplied system_prompt / model on a session.
        # Drop the prompt (server resolves the weakness from task_id) and bind the
        # model to the session's server-side ai_config so the candidate can't
        # override the weakness instructions or pick an arbitrary model.
        request.system_prompt = None
        if isinstance(row.ai_config, dict) and row.ai_config.get("primary_model"):
            request.model = row.ai_config["primary_model"]

    try:
        with session_usage(request.session_code):
            return await call_llm(request)
    except UsageLimitExceeded as exc:
        raise HTTPException(status_code=429, detail={"reason": "usage_limit"}) from exc
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ModelNotAllowed as e:
        raise HTTPException(status_code=400, detail={"reason": "model_not_allowed"}) from e
