"""OIDC routes (self-hosted mode; hardened).

Active only when VIBONY_AUTH_MODE=oidc. Discovery document and JWKS are fetched
lazily on first request and cached in module-level dicts. Tests clear the cache
via `oidc._discovery.clear(); oidc._jwks_data.clear()`.

Hardening:
- Per-flow random `state` + PKCE (S256), both bound to a short-lived signed
  HttpOnly flow cookie compared exactly on callback (login-CSRF defense).
- Per-flow random `nonce` echoed in the ID token, asserted on callback.
- Email-collision auto-linking only when the IdP marks the email verified.
- Callback URL is taken from VIBONY_BASE_URL only — never the Host header.
- ID-token signature algorithm pinned to an asymmetric allowlist (no `none`).
- Discovery/JWKS/token fetch failures → 502, never an unhandled 500.
- Discovery/JWKS/token endpoints must be https + non-private-IP (SSRF guard).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
import urllib.parse
from typing import Any

import httpx

logger = logging.getLogger(__name__)
from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose.errors import JoseError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vibony_backend.auth.cookies import _secure, get_session_secret, issue_cookie
from vibony_backend.db.engine import session_scope
from vibony_backend.db.models import Company, Membership, OAuthIdentity, User

_PROVIDER = "oidc"

# Short-lived signed HttpOnly cookie carrying the per-flow state/nonce/PKCE
# verifier. SameSite=Lax so it survives the cross-site top-level GET
# redirect back from the IdP; Strict would drop it on the callback. Path-scoped
# to the callback so it is sent only where it is read. Its Secure flag follows
# _secure() (VIBONY_COOKIE_SECURE), same as the session cookie — so a deploy
# serving OIDC over plain HTTP MUST keep VIBONY_COOKIE_SECURE=false, else the
# browser drops this cookie and every callback fails closed with invalid_state.
_FLOW_COOKIE = "vibony_oidc_flow"
_FLOW_PATH = "/auth/oidc/callback"
_FLOW_MAX_AGE = 300

# ID-token signature allowlist: asymmetric only — never `none`/HS* (kills
# the alg-confusion + unsigned-token attacks). Override via
# VIBONY_OIDC_ID_TOKEN_ALGS (comma-separated); `none` is always stripped.
_DEFAULT_ID_TOKEN_ALGS = (
    "RS256", "RS384", "RS512",
    "ES256", "ES384", "ES512",
    "PS256", "PS384", "PS512",
)

# Module-level cache — cleared between tests by clearing these dicts.
_discovery: dict[str, Any] = {}
_jwks_data: dict[str, Any] = {}


def _require_oidc() -> None:
    if os.getenv("VIBONY_AUTH_MODE", "password") != "oidc":
        raise HTTPException(status_code=404)


def _flow_serializer() -> URLSafeTimedSerializer:
    # Same ≥32-char gate as the cookie/email paths.
    return URLSafeTimedSerializer(get_session_secret(), salt="vibony-oidc-flow")


def _callback_uri() -> str:
    """Absolute callback URL from VIBONY_BASE_URL only.

    Never derived from request.base_url / the Host header: an attacker who can
    spoof Host would otherwise rewrite the redirect_uri (open-redirect / token
    exfiltration). Unset in OIDC mode → 500 (also fail-fast at startup)."""
    base = os.getenv("VIBONY_BASE_URL", "").strip()
    if not base:
        raise HTTPException(status_code=500, detail={"reason": "oidc_misconfigured"})
    return f"{base.rstrip('/')}/auth/oidc/callback"


def _id_token_algs() -> list[str]:
    raw = os.environ.get("VIBONY_OIDC_ID_TOKEN_ALGS", "").strip()
    if not raw:
        return list(_DEFAULT_ID_TOKEN_ALGS)
    algs = [a.strip() for a in raw.split(",") if a.strip() and a.strip().lower() != "none"]
    return algs or list(_DEFAULT_ID_TOKEN_ALGS)


def _allow_insecure_issuer() -> bool:
    return os.environ.get("VIBONY_OIDC_ALLOW_INSECURE_ISSUER", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _assert_fetchable_url(url: str) -> None:
    """SSRF guard for server-side issuer-derived fetches.

    The issuer is operator config, but discovery returns jwks_uri/token_endpoint
    that a malicious/compromised IdP could point at internal services. Require
    https and reject literal private/loopback/link-local IP hosts (catches the
    169.254.169.254 metadata vector). DNS-rebinding is not covered (no resolve
    here) — noted residual. Bypassed for dev/test via
    VIBONY_OIDC_ALLOW_INSECURE_ISSUER."""
    if _allow_insecure_issuer():
        return
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise HTTPException(status_code=500, detail={"reason": "oidc_misconfigured"})
    host = parsed.hostname or ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise HTTPException(status_code=500, detail={"reason": "oidc_misconfigured"})


async def _get_discovery() -> dict[str, Any]:
    if not _discovery:
        issuer = os.environ.get("VIBONY_OIDC_ISSUER", "").rstrip("/")
        if not issuer:
            raise HTTPException(status_code=500, detail={"reason": "oidc_misconfigured"})
        url = f"{issuer}/.well-known/openid-configuration"
        _assert_fetchable_url(url)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError:
            # Never surface an httpx traceback as a 500 on an unauthenticated path.
            raise HTTPException(status_code=502, detail={"reason": "oidc_upstream_unavailable"})
        _discovery.update(resp.json())
    return _discovery


async def _get_jwks() -> dict[str, Any]:
    if not _jwks_data:
        discovery = await _get_discovery()
        jwks_uri = discovery.get("jwks_uri", "")
        _assert_fetchable_url(jwks_uri)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(jwks_uri)
            resp.raise_for_status()
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail={"reason": "oidc_upstream_unavailable"})
        _jwks_data.update(resp.json())
    return _jwks_data


def _validate_id_token(
    id_token: str,
    jwks: dict[str, Any],
    issuer: str,
    client_id: str,
) -> dict[str, Any]:
    """Validate OIDC ID token signature + standard claims. Raises 401 on failure."""
    try:
        key_set = JsonWebKey.import_key_set(jwks)
        # Pin the accepted algorithms so a token with alg=none/HS256 is
        # rejected before signature verification (alg-confusion defense).
        # Mark `exp` essential — authlib's validate_exp only checks an
        # `exp` that is *present*, so a token omitting it would otherwise be
        # accepted as never-expiring. A missing `exp` now raises MissingClaimError
        # (a JoseError → caught below → 401).
        claims = JsonWebToken(_id_token_algs()).decode(
            id_token, key_set, claims_options={"exp": {"essential": True}}
        )
        claims.validate()
    except JoseError:
        # Do not surface exception text — it may contain decoded token fragments.
        raise HTTPException(status_code=401, detail={"reason": "invalid_id_token"})

    if claims.get("iss") != issuer:
        raise HTTPException(status_code=401, detail={"reason": "invalid_id_token"})
    aud = claims.get("aud")
    if isinstance(aud, str):
        aud = [aud]
    if client_id not in (aud or []):
        raise HTTPException(status_code=401, detail={"reason": "invalid_id_token"})
    return dict(claims)


def _claim_is_true(value: Any) -> bool:
    """Coerce an OIDC boolean claim (some IdPs send the string "true")."""
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


router = APIRouter(prefix="/auth/oidc", tags=["auth"])


@router.get("/start")
async def oidc_start(request: Request) -> RedirectResponse:
    _require_oidc()
    # Validate config before any network I/O (fail-fast on a missing base URL).
    redirect_uri = _callback_uri()
    discovery = await _get_discovery()

    # Per-flow secrets: random state + nonce + PKCE verifier, all bound
    # to the signed flow cookie below and compared exactly on the callback.
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )

    params = urllib.parse.urlencode({
        "client_id": os.environ.get("VIBONY_OIDC_CLIENT_ID", ""),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })
    redirect = RedirectResponse(
        url=f"{discovery['authorization_endpoint']}?{params}", status_code=302
    )
    payload = _flow_serializer().dumps({"s": state, "n": nonce, "v": code_verifier})
    redirect.set_cookie(
        key=_FLOW_COOKIE,
        value=payload,
        max_age=_FLOW_MAX_AGE,
        httponly=True,
        secure=_secure(),
        samesite="lax",
        path=_FLOW_PATH,
    )
    return redirect


@router.get("/callback")
async def oidc_callback(
    request: Request,
    code: str,
    state: str,
    db: AsyncSession = Depends(session_scope),
) -> RedirectResponse:
    _require_oidc()

    # The flow cookie is the browser-bound CSRF token. Missing/forged/expired
    # or a state mismatch → reject before any token exchange.
    raw_flow = request.cookies.get(_FLOW_COOKIE)
    if not raw_flow:
        raise HTTPException(status_code=400, detail={"reason": "invalid_state"})
    try:
        flow = _flow_serializer().loads(raw_flow, max_age=_FLOW_MAX_AGE)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=400, detail={"reason": "invalid_state"})
    if not isinstance(flow, dict) or not hmac.compare_digest(str(flow.get("s", "")), state):
        raise HTTPException(status_code=400, detail={"reason": "invalid_state"})
    expected_nonce = flow.get("n")
    code_verifier = flow.get("v")

    issuer = os.environ.get("VIBONY_OIDC_ISSUER", "").rstrip("/")
    client_id = os.environ.get("VIBONY_OIDC_CLIENT_ID", "")
    client_secret = os.environ.get("VIBONY_OIDC_CLIENT_SECRET", "")

    discovery = await _get_discovery()
    jwks = await _get_jwks()

    token_endpoint = discovery.get("token_endpoint", "")
    _assert_fetchable_url(token_endpoint)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            token_resp = await client.post(
                token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _callback_uri(),
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code_verifier": code_verifier,  # PKCE
                },
            )
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail={"reason": "oidc_upstream_unavailable"})

    if token_resp.status_code != 200:
        raise HTTPException(status_code=400, detail={"reason": "token_exchange_failed"})

    id_token = token_resp.json().get("id_token")
    if not id_token:
        raise HTTPException(status_code=400, detail={"reason": "token_exchange_failed"})

    claims = _validate_id_token(id_token, jwks, issuer, client_id)

    # The ID token must echo the per-flow nonce we sent in /start.
    if claims.get("nonce") != expected_nonce:
        raise HTTPException(status_code=401, detail={"reason": "invalid_id_token"})

    sub = claims.get("sub")
    email = claims.get("email")
    email_verified = _claim_is_true(claims.get("email_verified"))

    if not sub:
        raise HTTPException(status_code=400, detail={"reason": "missing_sub"})

    user = await jit_provision(db, subject=sub, email=email, email_verified=email_verified)
    redirect = RedirectResponse(url="/", status_code=302)
    issue_cookie(user.id, user.session_version, redirect)
    redirect.delete_cookie(_FLOW_COOKIE, path=_FLOW_PATH)
    return redirect


def _check_allowed_domain(email: str | None) -> None:
    """Enforce VIBONY_OIDC_ALLOWED_DOMAINS if set.

    Value: comma-separated list of email domains, e.g. ``corp.com,partner.com``.
    Unset (default) → no restriction; the IdP is the gate.
    Set → any identity whose email doesn't match a listed domain gets 403.
    An identity with no email claim is always rejected when domains are restricted.
    """
    raw = os.environ.get("VIBONY_OIDC_ALLOWED_DOMAINS", "").strip()
    if not raw:
        return  # no restriction
    allowed = {d.strip().lower() for d in raw.split(",") if d.strip()}
    if not allowed:
        return
    if not email:
        raise HTTPException(status_code=403, detail={"reason": "domain_not_allowed"})
    domain = email.split("@")[-1].lower()
    if domain not in allowed:
        raise HTTPException(status_code=403, detail={"reason": "domain_not_allowed"})


def _trust_unverified_email() -> bool:
    """Escape hatch for IdPs that don't emit the email_verified claim but are
    fully trusted by the operator (e.g. a corporate IdP that only admits
    pre-verified accounts). Default off → fail-closed."""
    return os.environ.get("VIBONY_OIDC_TRUST_EMAIL", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


async def jit_provision(
    db: AsyncSession, *, subject: str, email: str | None, email_verified: bool = False
) -> User:
    """JIT-provision a user for OIDC login.

    1. Domain check via VIBONY_OIDC_ALLOWED_DOMAINS (403 if rejected).
    2. OAuthIdentity hit → return existing User.
    3. Email collision → attach OAuthIdentity to existing User **only if the IdP
       marked the email verified**; otherwise 403 — auto-linking an
       unverified email to an existing (possibly password) account is an
       account-takeover primitive.
    4. Full miss + no companies → create Company + User + Membership(admin).
    5. Full miss + companies exist → use first company, create User + Membership(interviewer).

    Access control model: the IdP is the primary gate. VIBONY_OIDC_ALLOWED_DOMAINS
    is the app-level guard for operators whose IdP doesn't support group/domain
    restrictions natively (e.g. a public Keycloak realm).
    """
    _check_allowed_domain(email)
    identity = (
        await db.execute(
            select(OAuthIdentity).where(
                OAuthIdentity.provider == _PROVIDER,
                OAuthIdentity.subject == subject,
            )
        )
    ).scalar_one_or_none()

    if identity is not None:
        return (await db.execute(select(User).where(User.id == identity.user_id))).scalar_one()

    user: User | None = None
    if email:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()

    if user is not None:
        # Never link a fresh OIDC subject to an existing account on an
        # unverified email — that would let an attacker present a victim's email
        # on a self-service IdP and take over the matching Vibony account.
        if not (email_verified or _trust_unverified_email()):
            logger.warning(
                "oidc jit_provision: refused link to existing user on unverified email "
                "(sub=%r) — set VIBONY_OIDC_TRUST_EMAIL=1 only for a fully trusted IdP",
                subject,
            )
            raise HTTPException(status_code=403, detail={"reason": "email_not_verified"})
        try:
            db.add(OAuthIdentity(user_id=user.id, provider=_PROVIDER, subject=subject, email=email))
            await db.commit()
        except IntegrityError:
            # Concurrent request already linked this identity — rollback and return user.
            await db.rollback()
        return user

    # Reaching here means a brand-new OIDC subject with no matching
    # account, so we must INSERT a User — and users.email is NOT NULL. An IdP that
    # omitted the email claim can't be provisioned; reject cleanly instead of
    # 500-ing on the NOT NULL violation at flush. (An already-linked identity
    # returns above without needing an email in this particular token.)
    if not email:
        logger.warning(
            "oidc jit_provision: refused new-user provisioning without an email claim (sub=%r)",
            subject,
        )
        raise HTTPException(status_code=400, detail={"reason": "email_required"})

    # Exclude the system templates workspace: it's not a tenant, so it
    # must not satisfy the "installation already bootstrapped" check below (else a
    # genuine first OIDC login would wrongly join the templates workspace as
    # interviewer instead of bootstrapping its own).
    from vibony_backend.tasks.seed import SYSTEM_COMPANY_ID

    company_count = (
        await db.execute(
            select(func.count())
            .select_from(Company)
            .where(Company.id != SYSTEM_COMPANY_ID)
        )
    ).scalar_one()

    # Self-hosted model: one Company per installation. First login bootstraps it;
    # all subsequent logins join as interviewer. The IdP is the access gate — no
    # per-user Company creation here.
    if company_count == 0:
        company_name = os.environ.get("VIBONY_COMPANY_NAME", "Vibony")
        company = Company(name=company_name)
        db.add(company)
        role = "admin"
        logger.info("oidc jit_provision: created first company %r, user will be admin", company_name)
    else:
        company = (
            await db.execute(
                select(Company).where(Company.id != SYSTEM_COMPANY_ID).limit(1)
            )
        ).scalar_one()
        role = "interviewer"
        logger.info(
            "oidc jit_provision: provisioning sub=%r as interviewer in company_id=%s",
            subject, company.id,
        )

    user = User(email=email, password_hash=None)
    db.add(user)
    await db.flush()

    db.add(Membership(user_id=user.id, company_id=company.id, role=role))
    db.add(OAuthIdentity(user_id=user.id, provider=_PROVIDER, subject=subject, email=email))
    await db.commit()
    await db.refresh(user)
    return user
