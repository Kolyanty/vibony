"""Deployment posture guards.

Sync unit tests for the pure guards + async endpoint tests for /ready and the docs
gate (the async ones carry the session-loop mark — see CLAUDE.md test traps).
"""

from __future__ import annotations

import pytest

_session_loop = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# production config fail-closed
# ---------------------------------------------------------------------------

def test_production_guard_noop_outside_production(monkeypatch):
    monkeypatch.delenv("VIBONY_ENV", raising=False)
    from vibony_backend.deploy import assert_production_config, is_production

    assert is_production() is False
    assert_production_config()  # dev → never raises


def test_production_guard_flags_default_creds_and_missing_frontend(monkeypatch):
    monkeypatch.setenv("VIBONY_ENV", "production")
    monkeypatch.setenv(
        "VIBONY_DATABASE_URL", "postgresql+asyncpg://vibony:vibony@db:5432/vibony"
    )
    monkeypatch.delenv("VIBONY_FRONTEND_URL", raising=False)
    monkeypatch.delenv("VIBONY_COOKIE_SECURE", raising=False)
    from vibony_backend.deploy import (
        assert_production_config,
        production_config_violations,
    )

    problems = production_config_violations()
    joined = "\n".join(problems)
    assert "default creds" in joined
    assert "no TLS" in joined
    assert "VIBONY_FRONTEND_URL" in joined
    assert "VIBONY_COOKIE_SECURE" in joined
    with pytest.raises(RuntimeError):
        assert_production_config()


def test_production_guard_passes_with_hardened_config(monkeypatch):
    monkeypatch.setenv("VIBONY_ENV", "production")
    monkeypatch.setenv(
        "VIBONY_DATABASE_URL",
        "postgresql+asyncpg://app:s3cret@db:5432/vibony?ssl=verify-full",
    )
    monkeypatch.setenv("VIBONY_FRONTEND_URL", "https://app.vibony.io")
    monkeypatch.setenv("VIBONY_COOKIE_SECURE", "true")
    from vibony_backend.deploy import (
        assert_production_config,
        production_config_violations,
    )

    assert production_config_violations() == []
    assert_production_config()  # all good → no raise


def test_production_guard_rejects_disabled_tls(monkeypatch):
    # A present-but-disabled sslmode must NOT pass the TLS guard (false-pass fix).
    monkeypatch.setenv("VIBONY_ENV", "production")
    monkeypatch.setenv(
        "VIBONY_DATABASE_URL", "postgresql+asyncpg://app:s3cret@db/vibony?sslmode=disable"
    )
    monkeypatch.setenv("VIBONY_FRONTEND_URL", "https://app.vibony.io")
    monkeypatch.setenv("VIBONY_COOKIE_SECURE", "true")
    from vibony_backend.deploy import production_config_violations

    assert any("no TLS" in p for p in production_config_violations())


def test_production_guard_rejects_non_https_frontend(monkeypatch):
    monkeypatch.setenv("VIBONY_ENV", "production")
    monkeypatch.setenv(
        "VIBONY_DATABASE_URL", "postgresql+asyncpg://app:s3cret@db/vibony?sslmode=require"
    )
    monkeypatch.setenv("VIBONY_FRONTEND_URL", "http://app.vibony.io")
    monkeypatch.setenv("VIBONY_COOKIE_SECURE", "true")
    from vibony_backend.deploy import production_config_violations

    problems = production_config_violations()
    assert any("https URL" in p for p in problems)


# ---------------------------------------------------------------------------
# docs gated off by default; litellm telemetry off
# ---------------------------------------------------------------------------

def test_docs_disabled_by_default(monkeypatch):
    monkeypatch.delenv("VIBONY_ENABLE_DOCS", raising=False)
    from vibony_backend.deploy import docs_enabled

    assert docs_enabled() is False


def test_litellm_telemetry_disabled():
    import litellm

    import vibony_backend.proxy  # noqa: F401 — import sets litellm.telemetry = False

    assert litellm.telemetry is False


# ---------------------------------------------------------------------------
# SMTP STARTTLS uses a verifying TLS context
# ---------------------------------------------------------------------------

def test_smtp_starttls_uses_verifying_context(monkeypatch):
    monkeypatch.setenv("VIBONY_EMAIL_BACKEND", "smtp")
    monkeypatch.setenv("VIBONY_SMTP_HOST", "smtp.example.com")

    calls: dict = {}

    class _FakeSMTP:
        def __init__(self, host, port):
            calls["host"] = host

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            calls["starttls_context"] = context

        def login(self, *a):
            calls["login"] = True

        def send_message(self, msg):
            calls["sent"] = True

    monkeypatch.setattr("vibony_backend.auth.email.smtplib.SMTP", _FakeSMTP)
    from vibony_backend.auth import email

    email.send_email("to@example.com", "subj", "body")
    import ssl

    assert isinstance(calls.get("starttls_context"), ssl.SSLContext)
    # A default context verifies certs + hostname.
    assert calls["starttls_context"].verify_mode == ssl.CERT_REQUIRED
    assert calls["starttls_context"].check_hostname is True
    assert calls["sent"] is True


# ---------------------------------------------------------------------------
# docs gated off; /ready pings the DB
# ---------------------------------------------------------------------------

@_session_loop
async def test_openapi_schema_not_served_by_default(http_client):
    # Docs gate: with VIBONY_ENABLE_DOCS unset the schema 404s.
    assert (await http_client.get("/openapi.json")).status_code == 404
    assert (await http_client.get("/docs")).status_code == 404


@_session_loop
async def test_ready_endpoint_pings_db(http_client):
    resp = await http_client.get("/ready")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ready"}


@_session_loop
async def test_health_is_liveness_only(http_client):
    resp = await http_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
