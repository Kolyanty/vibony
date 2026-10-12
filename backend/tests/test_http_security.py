"""Security response headers + WS Origin guard + SameSite=Strict."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient

_session_loop = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Security headers on every response
# ---------------------------------------------------------------------------

def test_security_headers_present():
    from vibony_backend.main import app

    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]


# ---------------------------------------------------------------------------
# WS Origin allowlist (only enforced when configured)
# ---------------------------------------------------------------------------

def test_origin_allowed_unset_is_permissive(monkeypatch):
    from vibony_backend.sessions import ws

    monkeypatch.delenv("VIBONY_ALLOWED_ORIGINS", raising=False)
    fake = SimpleNamespace(headers={"origin": "http://anything"})
    assert ws._origin_allowed(fake) is True


def test_origin_allowed_enforced_when_set(monkeypatch):
    from vibony_backend.sessions import ws

    monkeypatch.setenv("VIBONY_ALLOWED_ORIGINS", "https://app.vibony.io, https://vibony.io")
    assert ws._origin_allowed(SimpleNamespace(headers={"origin": "https://app.vibony.io"})) is True
    assert ws._origin_allowed(SimpleNamespace(headers={"origin": "https://evil.example"})) is False
    assert ws._origin_allowed(SimpleNamespace(headers={})) is False  # no Origin → blocked


# ---------------------------------------------------------------------------
# Session cookie is SameSite=Strict
# ---------------------------------------------------------------------------

@_session_loop
async def test_login_cookie_is_samesite_strict(http_client: AsyncClient):
    await http_client.post(
        "/auth/signup",
        json={"email": "strict@example.com", "password": "password123", "company_name": "W"},
    )
    resp = await http_client.post(
        "/auth/login", json={"email": "strict@example.com", "password": "password123"}
    )
    assert resp.status_code == 200
    assert "samesite=strict" in resp.headers.get("set-cookie", "").lower()
