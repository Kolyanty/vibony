"""In-process rate limiter + request body-size guard."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from vibony_backend import ratelimit

_session_loop = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# _allow sliding window
# ---------------------------------------------------------------------------

def test_allow_window():
    ratelimit.reset()
    now = 1000.0
    # 3 per 10s
    assert all(ratelimit._allow("k", 3, 10, now + i * 0.1) for i in range(3))
    assert not ratelimit._allow("k", 3, 10, now + 0.4)  # 4th in-window → blocked
    # after the window slides, allowed again
    assert ratelimit._allow("k", 3, 10, now + 11)


def test_limiter_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("VIBONY_RATELIMIT_ENABLED", "0")
    assert ratelimit._enabled() is False


def test_client_ip_ignores_xff_unless_trusted(monkeypatch):
    from starlette.requests import Request

    def _req(xff: str | None) -> Request:
        headers = [(b"x-forwarded-for", xff.encode())] if xff else []
        scope = {"type": "http", "headers": headers, "client": ("10.0.0.5", 1234)}
        return Request(scope)

    monkeypatch.delenv("VIBONY_TRUST_PROXY_HEADERS", raising=False)
    # Spoofed XFF is ignored by default → peer address is used.
    assert ratelimit._client_ip(_req("1.2.3.4")) == "10.0.0.5"

    monkeypatch.setenv("VIBONY_TRUST_PROXY_HEADERS", "1")
    assert ratelimit._client_ip(_req("1.2.3.4")) == "1.2.3.4"
    # No header → peer even when trusting.
    assert ratelimit._client_ip(_req(None)) == "10.0.0.5"


def test_sweep_evicts_idle_keys():
    ratelimit.reset()
    ratelimit._allow("old", 5, 10, 1000.0)
    ratelimit._allow("fresh", 5, 10, 100_000.0)
    ratelimit._sweep(100_000.0)  # "old" is > _IDLE_SECONDS stale
    assert "old" not in ratelimit._hits
    assert "fresh" in ratelimit._hits
    ratelimit.reset()


def test_hard_cap_bounds_hits_under_active_flood(monkeypatch):
    """Under a flood of distinct *active* keys (none idle), idle-only
    eviction can't bound the dict — the hard cap must drop the oldest keys."""
    ratelimit.reset()
    monkeypatch.setattr(ratelimit, "_MAX_KEYS", 5)
    now = 1000.0
    for i in range(50):
        ratelimit._allow(f"k{i}", 5, 10, now)  # same timestamp → none ever idle
    # Without the hard cap all 50 would survive; with it, bounded to the cap
    # (+1 transient from the just-added key before the next sweep).
    assert len(ratelimit._hits) <= ratelimit._MAX_KEYS + 1
    ratelimit.reset()


def test_hard_cap_evicts_oldest_keys(monkeypatch):
    """The hard cap switched sorted() → heapq.nsmallest; it must still drop the
    keys whose most-recent hit is oldest, keeping the recently-active ones."""
    ratelimit.reset()
    monkeypatch.setattr(ratelimit, "_MAX_KEYS", 3)
    # Distinct, increasing last-hit timestamps; large window so none are pruned.
    for i, ts in enumerate([1000.0, 2000.0, 3000.0, 4000.0]):
        ratelimit._allow(f"k{i}", 5, 100_000.0, ts)
    ratelimit._sweep(4000.0)  # len 4 > cap 3 → evict the single oldest
    assert "k0" not in ratelimit._hits  # oldest last-hit (1000) dropped
    assert {"k1", "k2", "k3"} <= set(ratelimit._hits)  # newer keys survive
    ratelimit.reset()


# ---------------------------------------------------------------------------
# Body-size middleware
# ---------------------------------------------------------------------------

def test_body_size_limit_413():
    async def ok(_request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", ok, methods=["POST"])])
    app.add_middleware(ratelimit.BodySizeLimitMiddleware, max_bytes=10)
    client = TestClient(app)

    assert client.post("/", content=b"x" * 5).status_code == 200
    resp = client.post("/", content=b"x" * 50)
    assert resp.status_code == 413
    assert resp.json()["detail"]["reason"] == "payload_too_large"


# ---------------------------------------------------------------------------
# Live endpoint: login is throttled when the limiter is on
# ---------------------------------------------------------------------------

@_session_loop
async def test_login_rate_limited(http_client: AsyncClient, monkeypatch):
    monkeypatch.setenv("VIBONY_RATELIMIT_ENABLED", "1")
    ratelimit.reset()

    payload = {"email": "nobody@example.com", "password": "wrongpassword"}
    # login bucket = 10 / 60s; first 10 are 401 (bad creds), 11th is 429.
    statuses = []
    for _ in range(11):
        r = await http_client.post("/auth/login", json=payload)
        statuses.append(r.status_code)

    assert statuses[:10] == [401] * 10, statuses
    assert statuses[10] == 429
    ratelimit.reset()


@_session_loop
async def test_resend_rate_limit_is_per_user(http_client: AsyncClient, monkeypatch):
    """The verify-email resend bucket is keyed per-user, not per-IP, so
    two users behind one IP (the shared ASGI test client) don't share it."""
    monkeypatch.setenv("VIBONY_RATELIMIT_ENABLED", "1")
    ratelimit.reset()

    a = await http_client.post(
        "/auth/signup",
        json={"email": "nat-a@example.com", "password": "password123", "company_name": "A"},
    )
    assert a.status_code == 201, a.text
    b = await http_client.post(
        "/auth/signup",
        json={"email": "nat-b@example.com", "password": "password123", "company_name": "B"},
    )
    assert b.status_code == 201, b.text

    # User A exhausts its own resend bucket (3 / 60s).
    for _ in range(3):
        r = await http_client.post("/auth/verify-email/resend", cookies=a.cookies)
        assert r.status_code == 202, r.text
    blocked = await http_client.post("/auth/verify-email/resend", cookies=a.cookies)
    assert blocked.status_code == 429

    # User B shares A's client IP but must still have its own (un-exhausted) bucket.
    r_b = await http_client.post("/auth/verify-email/resend", cookies=b.cookies)
    assert r_b.status_code == 202, r_b.text
    ratelimit.reset()
