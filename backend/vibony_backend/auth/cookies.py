"""Signed-cookie session helpers (itsdangerous, NOT JWT).

Cookie value = URLSafeTimedSerializer.dumps("<user_id>:<session_version>").
TTL enforced at read time (max_age=COOKIE_MAX_AGE). The embedded session_version
is compared against the stored one by the auth deps so a logout /
password change can revoke outstanding cookies server-side.
"""

from __future__ import annotations

import os
import uuid

from fastapi import Response
from itsdangerous import URLSafeTimedSerializer
from starlette.requests import HTTPConnection

COOKIE_MAX_AGE = 14 * 24 * 3600  # 14 days

# Minimum entropy for the signing secret. The secret signs session
# cookies + OIDC state + email-verify tokens; a weak one forges all three.
MIN_SECRET_LENGTH = 32


def get_session_secret() -> str:
    """The signing secret, or raise if missing/too weak. Single source so the
    cookie, OIDC-state and email-verify paths agree (no raw os.environ KeyError)."""
    secret = os.environ.get("VIBONY_SESSION_SECRET", "")
    if len(secret) < MIN_SECRET_LENGTH:
        raise RuntimeError(
            f"VIBONY_SESSION_SECRET must be set to at least {MIN_SECRET_LENGTH} "
            f"characters of entropy (got {len(secret)})."
        )
    return secret


def validate_session_secret() -> None:
    """Fail-fast at startup if the secret is missing/weak (called from lifespan)."""
    get_session_secret()


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_session_secret(), salt="vibony-session")


def _secure() -> bool:
    return os.environ.get("VIBONY_COOKIE_SECURE", "true").lower() not in (
        "false",
        "0",
        "no",
    )


def _cookie_name(secure: bool) -> str:
    """`__Host-` prefix requires Secure + Path=/ + no Domain — valid only
    over HTTPS, so use it when secure and a plain name in dev (HTTP)."""
    return "__Host-vibony_session" if secure else "vibony_session"


# Back-compat export: code/tests that referenced COOKIE_NAME get the dev name.
COOKIE_NAME = "vibony_session"


def issue_cookie(
    user_id: uuid.UUID, session_version: int, response: Response
) -> None:
    token = _serializer().dumps(f"{user_id}:{session_version}")
    secure = _secure()
    # __Host- mandates no Domain (host-only) — ignore VIBONY_COOKIE_DOMAIN when
    # secure so a shared parent domain can't scope/overwrite the cookie.
    domain = None if secure else (os.environ.get("VIBONY_COOKIE_DOMAIN") or None)
    response.set_cookie(
        key=_cookie_name(secure),
        value=token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=secure,
        samesite="strict",
        domain=domain,
        path="/",
    )


def read_cookie(request: HTTPConnection) -> tuple[uuid.UUID, int] | None:
    """Return (user_id, session_version) from the session cookie, or None.

    The caller (auth deps) must still compare session_version against the stored
    user.session_version to honor revocation."""
    token = request.cookies.get(_cookie_name(_secure()))
    if not token:
        return None
    try:
        payload = _serializer().loads(token, max_age=COOKIE_MAX_AGE)
        user_id_str, _, sv_str = payload.partition(":")
        return uuid.UUID(user_id_str), int(sv_str or "0")
    except Exception:
        return None


def clear_cookie(response: Response) -> None:
    secure = _secure()
    domain = None if secure else (os.environ.get("VIBONY_COOKIE_DOMAIN") or None)
    response.delete_cookie(
        key=_cookie_name(secure),
        httponly=True,
        secure=secure,
        samesite="strict",
        domain=domain,
        path="/",
    )
