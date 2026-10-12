"""Email sending + verification tokens.

Two pieces:

- **Stateless verification token:** an itsdangerous `URLSafeTimedSerializer`
  signed with `VIBONY_SESSION_SECRET` (same secret as the session cookie, distinct
  salt). The token *is* the proof — no DB token column; `users.email_verified_at`
  only records the result.
- **Pluggable sender:** `send_email()` dispatches to a backend selected by
  `VIBONY_EMAIL_BACKEND` — `console` (default; logs the body + link, used in dev +
  tests) or `smtp` (env-configured `VIBONY_SMTP_*`). Real SMTP delivery is wired but
  not exercised in CI (same posture as the gVisor opt-in). The provider choice is a
  deploy decision, out of scope here.

All sends are best-effort at the call site: a failure to send must never break
signup (the user can resend from the dashboard banner).
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
import uuid
from email.message import EmailMessage

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

logger = logging.getLogger(__name__)

_EMAIL_VERIFY_SALT = "vibony-email-verify"
# 24h to confirm; expired tokens are re-issuable via /auth/verify-email/resend.
EMAIL_VERIFY_MAX_AGE_SECONDS = 24 * 60 * 60


def _serializer() -> URLSafeTimedSerializer:
    # Shared getter: validates length, no raw-KeyError path.
    from vibony_backend.auth.cookies import get_session_secret

    return URLSafeTimedSerializer(get_session_secret(), salt=_EMAIL_VERIFY_SALT)


def make_email_token(user_id: uuid.UUID) -> str:
    return _serializer().dumps(str(user_id))


def read_email_token(token: str) -> uuid.UUID | None:
    """Return the user id encoded in a valid, unexpired token, else None."""
    try:
        raw = _serializer().loads(token, max_age=EMAIL_VERIFY_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError):
        return None


def _frontend_url() -> str:
    return os.getenv("VIBONY_FRONTEND_URL", "http://localhost:5173").rstrip("/")


def verification_link(token: str) -> str:
    return f"{_frontend_url()}/verify-email?token={token}"


# -------- sender backends --------


def _send_console(to: str, subject: str, text_body: str) -> None:
    logger.info("EMAIL (console backend) to=%s subject=%s\n%s", to, subject, text_body)


def _send_smtp(to: str, subject: str, text_body: str) -> None:
    host = os.environ["VIBONY_SMTP_HOST"]
    port = int(os.getenv("VIBONY_SMTP_PORT", "587"))
    user = os.getenv("VIBONY_SMTP_USER")
    password = os.getenv("VIBONY_SMTP_PASSWORD")
    sender = os.getenv("VIBONY_SMTP_FROM", user or "no-reply@vibony.local")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text_body)

    # STARTTLS with certificate + hostname verification (the bare
    # starttls() does neither, so an active MITM could strip/forge the TLS and read
    # the credentials + link). create_default_context() verifies the server cert.
    context = ssl.create_default_context()
    with smtplib.SMTP(host, port) as server:
        server.starttls(context=context)
        if user and password:
            server.login(user, password)
        server.send_message(msg)


def send_email(to: str, subject: str, text_body: str) -> None:
    """Dispatch an email via the configured backend. Raises on transport failure;
    callers that must not break on send failure should wrap in try/except."""
    backend = os.getenv("VIBONY_EMAIL_BACKEND", "console").lower()
    if backend == "smtp":
        _send_smtp(to, subject, text_body)
    else:
        _send_console(to, subject, text_body)


def send_verification_email(to: str, user_id: uuid.UUID) -> None:
    """Best-effort: build + send the verification email; never raises."""
    try:
        link = verification_link(make_email_token(user_id))
        send_email(
            to,
            "Verify your Vibony email",
            f"Welcome to Vibony!\n\nConfirm your email address:\n{link}\n\n"
            "This link expires in 24 hours. If you didn't sign up, ignore this message.",
        )
    except Exception as exc:  # noqa: BLE001 — delivery must never break signup
        logger.warning("verification email to %s failed: %s", to, exc)
