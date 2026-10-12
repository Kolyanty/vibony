"""In-process rate limiting + request body-size guard.

In-memory sliding-window counters keyed by ``name:client-ip``. Single-process
only — like the broker / agent_host / async_timer in-process singletons — so a
future multi-worker deploy needs a shared store (Redis); that caveat is tracked
in the multi-worker findings. Disabled when
``VIBONY_RATELIMIT_ENABLED`` is falsey: the test suite turns it off so existing
tests aren't throttled, and dedicated tests flip it on.
"""

from __future__ import annotations

import heapq
import os
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

_hits: dict[str, deque[float]] = defaultdict(deque)

# Idle-key eviction (avoid unbounded growth under rotating keys). When the dict
# exceeds _MAX_KEYS, drop keys with no hit in the last _IDLE_SECONDS.
_MAX_KEYS = 10_000
_IDLE_SECONDS = 3600.0


def _enabled() -> bool:
    return os.getenv("VIBONY_RATELIMIT_ENABLED", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _trust_proxy_headers() -> bool:
    return os.getenv("VIBONY_TRUST_PROXY_HEADERS", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _client_ip(request: Request) -> str:
    """Client IP for the bucket key. The peer address by default — a client can
    forge X-Forwarded-For, so only the first hop is trusted when the deploy
    explicitly opts in (VIBONY_TRUST_PROXY_HEADERS, set behind a known proxy that
    overwrites the header). Otherwise XFF is ignored to keep the limiter effective."""
    if _trust_proxy_headers():
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _sweep(now: float) -> None:
    cutoff = now - _IDLE_SECONDS
    stale = [k for k, dq in _hits.items() if not dq or dq[-1] < cutoff]
    for k in stale:
        del _hits[k]
    # Idle eviction alone can't bound growth under a sustained flood of
    # distinct *active* keys (none are idle) — the dict grows without limit. Hard
    # cap: if still over, drop the keys whose most-recent hit is oldest until back
    # under the cap. A dropped key just loses its window early and is re-created on
    # its next request — an acceptable trade vs unbounded memory.
    if len(_hits) > _MAX_KEYS:
        overflow = len(_hits) - _MAX_KEYS
        # heapq.nsmallest is O(N log overflow) vs sorted()'s O(N log N) — overflow
        # is usually 1 at the cap, so this avoids a full-dict sort on every
        # distinct-key request once _hits sits at the ceiling under a flood.
        oldest = heapq.nsmallest(
            overflow, _hits.items(), key=lambda kv: (kv[1][-1] if kv[1] else 0.0)
        )
        for k, _dq in oldest:
            del _hits[k]


def _allow(key: str, limit: int, window_s: float, now: float) -> bool:
    if len(_hits) > _MAX_KEYS:
        _sweep(now)
    dq = _hits[key]
    cutoff = now - window_s
    while dq and dq[0] < cutoff:
        dq.popleft()
    if len(dq) >= limit:
        return False
    dq.append(now)
    return True


def _reject(window_s: float) -> None:
    raise HTTPException(
        status_code=429,
        detail={"reason": "rate_limited"},
        headers={"Retry-After": str(int(window_s))},
    )


def rate_limit(name: str, limit: int, window_s: float):
    """FastAPI dependency: allow ``limit`` requests per ``window_s`` per client IP
    for the ``name`` bucket, else 429. No-op when the limiter is disabled."""

    async def _dep(request: Request) -> None:
        if not _enabled():
            return
        key = f"{name}:{_client_ip(request)}"
        if not _allow(key, limit, window_s, time.monotonic()):
            _reject(window_s)

    return _dep


def rate_limit_user(name: str, limit: int, window_s: float):
    """Like ``rate_limit`` but keyed by the authenticated user id, not the client
    IP — users behind a shared NAT must not share a bucket. Resolves
    ``current_user`` first (401 if unauthenticated), then checks the limit.

    ``current_user`` is imported lazily: ratelimit is a low-level module and must
    not import the auth layer at module load (cycle)."""
    from vibony_backend.auth.deps import current_user

    async def _dep(user=Depends(current_user)) -> None:
        if not _enabled():
            return
        key = f"{name}:user:{user.id}"
        if not _allow(key, limit, window_s, time.monotonic()):
            _reject(window_s)

    return _dep


def reset() -> None:
    """Clear all counters (tests)."""
    _hits.clear()


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add baseline security response headers.

    `frame-ancestors 'none'` + X-Frame-Options anti-clickjacking, nosniff, and a
    no-referrer policy (defense-in-depth for tokens that still ride URLs).
    CSP is intentionally minimal (`frame-ancestors` only) so it doesn't break the
    Swagger UI at /docs. HSTS is emitted only when cookies are Secure (an https
    deploy); the TLS proxy may also set it."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        # HSTS only on an https deploy — reuse the cookie module's single
        # "is this a secure deploy" predicate so headers + cookies never diverge.
        from vibony_backend.auth.cookies import _secure

        if _secure():
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Content-Length exceeds ``max_bytes`` with 413.

    Content-Length-based: a chunked request without the header bypasses this, so
    uvicorn's own limits remain the backstop for that case. Covers the common
    oversized-JSON DoS (huge messages/tools/starter_files)."""

    def __init__(self, app, max_bytes: int) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > self.max_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": {"reason": "payload_too_large"}},
                    )
            except ValueError:
                pass
        return await call_next(request)
