"""Deployment posture guards.

A small `VIBONY_ENV` notion (`development` default, `production` opt-in) drives the
fail-closed startup checks that a prod deploy must satisfy but a dev box must not be
burdened with. Centralised here so `main.py`'s lifespan calls one function.

Guarantees:
- Refuse the committed default DB creds (`vibony:vibony`) + require TLS in
  the connection string in production.
- Require `VIBONY_FRONTEND_URL` set to an https URL in production (it seeds
  invite / verify / candidate links).
- Require `VIBONY_COOKIE_SECURE=true` in production.
"""

from __future__ import annotations

import os

_TRUE = {"1", "true", "yes", "on"}
# Committed dev default the production deploy must rotate off. Anchored on the
# trailing "@" so it only matches the user:password segment of a connection URL.
_DEFAULT_DB_CRED = "vibony:vibony@"
# TLS sslmode/ssl values that do NOT guarantee an encrypted connection — a URL using
# one of these must not pass the TLS guard.
_TLS_OFF_VALUES = {"disable", "false", "0", "off", "allow", "prefer", ""}


def is_production() -> bool:
    """True when ``VIBONY_ENV`` names a production-like environment."""
    return os.getenv("VIBONY_ENV", "development").strip().lower() in (
        "production",
        "prod",
    )


def _env_true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUE


def docs_enabled() -> bool:
    """The interactive API docs (/docs, /redoc, /openapi.json) are off unless
    explicitly enabled — so a prod deploy never exposes the schema by default."""
    return _env_true("VIBONY_ENABLE_DOCS")


def _db_uses_default_creds(url: str) -> bool:
    return _DEFAULT_DB_CRED in url


def _db_has_tls(url: str) -> bool:
    """asyncpg takes TLS via an ``ssl=`` / ``sslmode=`` query arg (or a connect_arg);
    we only check the URL form an operator would set. A present-but-disabled value
    (e.g. ``?sslmode=disable``/``?ssl=prefer``) is treated as NOT TLS so it can't
    sneak past the guard the way a bare substring check would."""
    lowered = url.lower()
    for key in ("sslmode=", "ssl="):
        idx = lowered.find(key)
        if idx == -1:
            continue
        value = lowered[idx + len(key) :].split("&", 1)[0].strip()
        if value not in _TLS_OFF_VALUES:
            return True
    return False


def production_config_violations() -> list[str]:
    """Return the list of production-posture problems (empty = OK). Split out from
    the asserting wrapper so it is trivially unit-testable."""
    problems: list[str] = []

    db_url = os.getenv("VIBONY_DATABASE_URL", "")
    if db_url and _db_uses_default_creds(db_url):
        problems.append(
            "VIBONY_DATABASE_URL still uses the committed default creds "
            "(vibony:vibony) — rotate them"
        )
    if db_url and not _db_has_tls(db_url):
        problems.append(
            "VIBONY_DATABASE_URL has no TLS — append ?ssl=verify-full (or sslmode=) "
            "so candidate PII is not sent to the DB in cleartext"
        )

    frontend = os.getenv("VIBONY_FRONTEND_URL", "").strip()
    if not frontend:
        problems.append("VIBONY_FRONTEND_URL must be set in production")
    elif not frontend.lower().startswith("https://"):
        problems.append("VIBONY_FRONTEND_URL must be an https URL in production")

    if not _env_true("VIBONY_COOKIE_SECURE"):
        problems.append(
            "VIBONY_COOKIE_SECURE must be true in production (Secure session cookie)"
        )

    return problems


def assert_production_config() -> None:
    """Fail-closed in production: raise ``RuntimeError`` listing every posture
    violation so a misconfigured prod deploy never boots. No-op outside production."""
    if not is_production():
        return
    problems = production_config_violations()
    if problems:
        raise RuntimeError(
            "VIBONY_ENV=production fail-closed — fix:\n  - " + "\n  - ".join(problems)
        )
