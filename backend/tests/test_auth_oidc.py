"""OIDC self-hosted auth tests (+ hardening).

pytest-httpx intercepts the httpx calls made by the handler (discovery, JWKS,
token endpoint). The ASGI-backed http_client is unaffected.

The fake OIDC issuer lives at http://fake-oidc.test. RSA keys come from the
session-scoped ``oidc_test_keys`` fixture in conftest.py.

The reworked flow: ``/start`` now mints a per-flow random state/nonce/PKCE
verifier bound to a signed HttpOnly cookie, so the callback tests drive ``/start``
first (``_run_flow``) instead of forging a constant state.
"""
from __future__ import annotations

import time
import urllib.parse

import pytest
from httpx import AsyncClient
from pytest_httpx import HTTPXMock

from authlib.jose import jwt as jose_jwt

pytestmark = pytest.mark.asyncio(loop_scope="session")

_ISSUER = "http://fake-oidc.test"
_CLIENT_ID = "vibony-test"
_DISCOVERY_URL = f"{_ISSUER}/.well-known/openid-configuration"
_JWKS_URL = f"{_ISSUER}/jwks"
_TOKEN_URL = f"{_ISSUER}/token"
_AUTH_URL = f"{_ISSUER}/auth"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBONY_SESSION_SECRET", "test-secret-" + "x" * 40)
    monkeypatch.setenv("VIBONY_COOKIE_SECURE", "false")
    monkeypatch.delenv("VIBONY_GOOGLE_CLIENT_ID", raising=False)


@pytest.fixture(autouse=True)
def clear_oidc_cache() -> None:
    from vibony_backend.auth import oidc
    oidc._discovery.clear()
    oidc._jwks_data.clear()


def _oidc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBONY_AUTH_MODE", "oidc")
    monkeypatch.setenv("VIBONY_OIDC_ISSUER", _ISSUER)
    monkeypatch.setenv("VIBONY_OIDC_CLIENT_ID", _CLIENT_ID)
    monkeypatch.setenv("VIBONY_OIDC_CLIENT_SECRET", "test-secret")
    # Callback URL comes from a fixed base; the fake issuer is http,
    # so opt out of the https/private-IP SSRF guard for the test issuer.
    monkeypatch.setenv("VIBONY_BASE_URL", "http://test")
    monkeypatch.setenv("VIBONY_OIDC_ALLOW_INSECURE_ISSUER", "1")


def _mock_discovery(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_DISCOVERY_URL,
        json={
            "issuer": _ISSUER,
            "authorization_endpoint": _AUTH_URL,
            "token_endpoint": _TOKEN_URL,
            "jwks_uri": _JWKS_URL,
        },
    )


def _mock_jwks(httpx_mock: HTTPXMock, oidc_test_keys: dict) -> None:
    httpx_mock.add_response(url=_JWKS_URL, json=oidc_test_keys["jwks"])


def _mock_token(httpx_mock: HTTPXMock, id_token: str) -> None:
    httpx_mock.add_response(url=_TOKEN_URL, json={"id_token": id_token})


def _make_id_token(
    oidc_test_keys: dict,
    sub: str,
    email: str | None = None,
    *,
    nonce: str | None = None,
    email_verified: bool | None = None,
    issuer: str = _ISSUER,
    client_id: str = _CLIENT_ID,
    expired: bool = False,
) -> str:
    now = int(time.time())
    payload: dict = {
        "iss": issuer,
        "sub": sub,
        "aud": client_id,
        "iat": now,
        "exp": now - 10 if expired else now + 300,
    }
    if email:
        payload["email"] = email
    if nonce is not None:
        payload["nonce"] = nonce
    if email_verified is not None:
        payload["email_verified"] = email_verified
    token = jose_jwt.encode(
        {"alg": "RS256", "kid": "test-key-1"},
        payload,
        oidc_test_keys["jwk"],
    )
    return token.decode("utf-8") if isinstance(token, bytes) else token


async def _start_flow(http_client: AsyncClient) -> tuple[str, str, dict[str, str]]:
    """Drive GET /start → return (state, nonce, {flow_cookie}).

    Caller must mock the discovery document first (cached after the first call).
    """
    from vibony_backend.auth.oidc import _FLOW_COOKIE, _FLOW_MAX_AGE, _flow_serializer

    r = await http_client.get("/auth/oidc/start", follow_redirects=False)
    assert r.status_code == 302, r.text
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)
    state = qs["state"][0]
    raw = r.cookies[_FLOW_COOKIE]
    flow = _flow_serializer().loads(raw, max_age=_FLOW_MAX_AGE)
    # PKCE challenge + nonce must be present on the authorization request.
    assert qs["code_challenge_method"][0] == "S256"
    assert qs["code_challenge"][0]
    assert qs["nonce"][0] == flow["n"]
    return state, flow["n"], {_FLOW_COOKIE: raw}


# ---------------------------------------------------------------------------
# 1. /auth/config reflects mode
# ---------------------------------------------------------------------------

async def test_auth_config_password_mode(
    http_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBONY_AUTH_MODE", "password")
    r = await http_client.get("/auth/config")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "password"
    assert body["oidc_login_url"] is None


async def test_auth_config_oidc_mode(
    http_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _oidc_env(monkeypatch)
    r = await http_client.get("/auth/config")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "oidc"
    assert body["oidc_login_url"] == "/auth/oidc/start"


# ---------------------------------------------------------------------------
# 2. /auth/signup returns 404 in oidc mode
# ---------------------------------------------------------------------------

async def test_signup_404_in_oidc_mode(
    http_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _oidc_env(monkeypatch)
    r = await http_client.post(
        "/auth/signup",
        json={"email": "x@x.com", "password": "password123", "company_name": "X"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 3. First OIDC login → creates Company + admin
# ---------------------------------------------------------------------------

async def test_oidc_first_login_creates_company_and_admin(
    http_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    oidc_test_keys: dict,
) -> None:
    _oidc_env(monkeypatch)
    monkeypatch.setenv("VIBONY_COMPANY_NAME", "AcmeCorp")

    _mock_discovery(httpx_mock)
    _mock_jwks(httpx_mock, oidc_test_keys)
    state, nonce, cookies = await _start_flow(http_client)
    _mock_token(httpx_mock, _make_id_token(oidc_test_keys, "sub-001", "admin@acme.com", nonce=nonce))

    r = await http_client.get(
        "/auth/oidc/callback",
        params={"code": "code1", "state": state},
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/"
    assert "vibony_session" in r.cookies

    me = await http_client.get("/auth/me", cookies=r.cookies)
    assert me.status_code == 200
    body = me.json()
    assert body["user"]["email"] == "admin@acme.com"
    assert len(body["memberships"]) == 1
    assert body["memberships"][0]["role"] == "admin"
    assert body["memberships"][0]["company_name"] == "AcmeCorp"


# ---------------------------------------------------------------------------
# 4. Second OIDC login (different user) → joins existing Company as interviewer
# ---------------------------------------------------------------------------

async def test_oidc_second_login_joins_as_interviewer(
    http_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    oidc_test_keys: dict,
) -> None:
    _oidc_env(monkeypatch)

    # First login — creates company + admin.
    _mock_discovery(httpx_mock)
    _mock_jwks(httpx_mock, oidc_test_keys)
    state1, nonce1, cookies1 = await _start_flow(http_client)
    _mock_token(httpx_mock, _make_id_token(oidc_test_keys, "sub-admin", "admin@corp.com", nonce=nonce1))

    r1 = await http_client.get(
        "/auth/oidc/callback",
        params={"code": "code-admin", "state": state1},
        cookies=cookies1,
        follow_redirects=False,
    )
    assert r1.status_code == 302

    # Second login — different user, company already exists.
    # Discovery and JWKS are now cached; only token needs mocking.
    state2, nonce2, cookies2 = await _start_flow(http_client)
    _mock_token(
        httpx_mock,
        _make_id_token(oidc_test_keys, "sub-interviewer", "interviewer@corp.com", nonce=nonce2),
    )

    r2 = await http_client.get(
        "/auth/oidc/callback",
        params={"code": "code-interviewer", "state": state2},
        cookies=cookies2,
        follow_redirects=False,
    )
    assert r2.status_code == 302

    me2 = await http_client.get("/auth/me", cookies=r2.cookies)
    assert me2.status_code == 200
    body = me2.json()
    assert body["user"]["email"] == "interviewer@corp.com"
    assert len(body["memberships"]) == 1
    assert body["memberships"][0]["role"] == "interviewer"
    # Same company as admin — not a new one.
    me1 = await http_client.get("/auth/me", cookies=r1.cookies)
    assert me1.json()["memberships"][0]["company_name"] == body["memberships"][0]["company_name"]


# ---------------------------------------------------------------------------
# 5. VIBONY_OIDC_ALLOWED_DOMAINS rejects out-of-domain identities
# ---------------------------------------------------------------------------

async def test_oidc_allowed_domains_rejects_foreign_email(
    http_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    oidc_test_keys: dict,
) -> None:
    _oidc_env(monkeypatch)
    monkeypatch.setenv("VIBONY_OIDC_ALLOWED_DOMAINS", "corp.com,partner.com")

    _mock_discovery(httpx_mock)
    _mock_jwks(httpx_mock, oidc_test_keys)
    state, nonce, cookies = await _start_flow(http_client)
    _mock_token(httpx_mock, _make_id_token(oidc_test_keys, "sub-outsider", "outsider@evil.com", nonce=nonce))

    r = await http_client.get(
        "/auth/oidc/callback",
        params={"code": "code-outsider", "state": state},
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert r.json()["detail"]["reason"] == "domain_not_allowed"


async def test_oidc_allowed_domains_admits_matching_email(
    http_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    oidc_test_keys: dict,
) -> None:
    _oidc_env(monkeypatch)
    monkeypatch.setenv("VIBONY_OIDC_ALLOWED_DOMAINS", "corp.com")

    _mock_discovery(httpx_mock)
    _mock_jwks(httpx_mock, oidc_test_keys)
    state, nonce, cookies = await _start_flow(http_client)
    _mock_token(httpx_mock, _make_id_token(oidc_test_keys, "sub-insider", "insider@corp.com", nonce=nonce))

    r = await http_client.get(
        "/auth/oidc/callback",
        params={"code": "code-insider", "state": state},
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 302


# ---------------------------------------------------------------------------
# 6. Bad ID token signature → 401, no User row created
# ---------------------------------------------------------------------------

async def test_oidc_bad_token_signature_returns_401(
    http_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    oidc_test_keys: dict,
) -> None:
    _oidc_env(monkeypatch)

    # Create a token signed with a DIFFERENT key (kid still points to test-key-1,
    # so authlib will find the public key but signature verification fails).
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from authlib.jose import JsonWebKey

    wrong_private = rsa.generate_private_key(65537, 2048, default_backend())
    wrong_pem = wrong_private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    wrong_jwk = JsonWebKey.import_key(wrong_pem, {"kid": "test-key-1", "alg": "RS256"})

    _mock_discovery(httpx_mock)
    _mock_jwks(httpx_mock, oidc_test_keys)
    state, nonce, cookies = await _start_flow(http_client)

    now = int(time.time())
    bad_payload = {
        "iss": _ISSUER,
        "sub": "evil-sub",
        "aud": _CLIENT_ID,
        "iat": now,
        "exp": now + 300,
        "email": "evil@evil.com",
        "nonce": nonce,
    }
    bad_token = jose_jwt.encode({"alg": "RS256", "kid": "test-key-1"}, bad_payload, wrong_jwk)
    bad_token_str = bad_token.decode("utf-8") if isinstance(bad_token, bytes) else bad_token
    _mock_token(httpx_mock, bad_token_str)

    r = await http_client.get(
        "/auth/oidc/callback",
        params={"code": "evil-code", "state": state},
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert r.json()["detail"]["reason"] == "invalid_id_token"


# ---------------------------------------------------------------------------
# 7. State / flow-cookie binding (login-CSRF defense)
# ---------------------------------------------------------------------------

async def test_oidc_callback_without_flow_cookie_rejected(
    http_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _oidc_env(monkeypatch)
    # No /start, no flow cookie — a forged callback must not be honored.
    r = await http_client.get(
        "/auth/oidc/callback",
        params={"code": "x", "state": "anything"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "invalid_state"


async def test_oidc_callback_state_mismatch_rejected(
    http_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _oidc_env(monkeypatch)
    _mock_discovery(httpx_mock)
    _state, _nonce, cookies = await _start_flow(http_client)
    # Valid flow cookie, but the returned state doesn't match the cookie's.
    r = await http_client.get(
        "/auth/oidc/callback",
        params={"code": "x", "state": "attacker-supplied-state"},
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "invalid_state"


# ---------------------------------------------------------------------------
# 8. Nonce binding
# ---------------------------------------------------------------------------

async def test_oidc_nonce_mismatch_rejected(
    http_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    oidc_test_keys: dict,
) -> None:
    _oidc_env(monkeypatch)
    _mock_discovery(httpx_mock)
    _mock_jwks(httpx_mock, oidc_test_keys)
    state, _nonce, cookies = await _start_flow(http_client)
    # ID token carries a different nonce than the one issued in /start.
    _mock_token(
        httpx_mock,
        _make_id_token(oidc_test_keys, "sub-replay", "x@corp.com", nonce="wrong-nonce"),
    )
    r = await http_client.get(
        "/auth/oidc/callback",
        params={"code": "x", "state": state},
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert r.json()["detail"]["reason"] == "invalid_id_token"


# ---------------------------------------------------------------------------
# 9. Signing-algorithm pinning (reject HS256 / alg-confusion)
# ---------------------------------------------------------------------------

async def test_oidc_id_token_symmetric_alg_rejected(
    http_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    oidc_test_keys: dict,
) -> None:
    _oidc_env(monkeypatch)
    _mock_discovery(httpx_mock)
    _mock_jwks(httpx_mock, oidc_test_keys)
    state, nonce, cookies = await _start_flow(http_client)

    now = int(time.time())
    payload = {
        "iss": _ISSUER,
        "sub": "alg-confusion",
        "aud": _CLIENT_ID,
        "iat": now,
        "exp": now + 300,
        "email": "x@corp.com",
        "nonce": nonce,
    }
    hs_token = jose_jwt.encode({"alg": "HS256"}, payload, "shared-secret")
    hs_token_str = hs_token.decode("utf-8") if isinstance(hs_token, bytes) else hs_token
    _mock_token(httpx_mock, hs_token_str)

    r = await http_client.get(
        "/auth/oidc/callback",
        params={"code": "x", "state": state},
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert r.json()["detail"]["reason"] == "invalid_id_token"


# ---------------------------------------------------------------------------
# 10. Email-collision auto-link requires a verified email
# ---------------------------------------------------------------------------

async def _provision_existing_user(
    http_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, email: str
) -> None:
    """Create a password-mode account (the takeover target) before OIDC login."""
    monkeypatch.setenv("VIBONY_AUTH_MODE", "password")
    r = await http_client.post(
        "/auth/signup",
        json={"email": email, "password": "password123", "company_name": "Target"},
    )
    assert r.status_code == 201, r.text


async def test_oidc_email_collision_unverified_refused(
    http_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    oidc_test_keys: dict,
) -> None:
    await _provision_existing_user(http_client, monkeypatch, "victim@corp.com")

    _oidc_env(monkeypatch)
    _mock_discovery(httpx_mock)
    _mock_jwks(httpx_mock, oidc_test_keys)
    state, nonce, cookies = await _start_flow(http_client)
    # Attacker presents the victim's email with email_verified=false.
    _mock_token(
        httpx_mock,
        _make_id_token(
            oidc_test_keys, "attacker-sub", "victim@corp.com", nonce=nonce, email_verified=False
        ),
    )
    r = await http_client.get(
        "/auth/oidc/callback",
        params={"code": "x", "state": state},
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert r.json()["detail"]["reason"] == "email_not_verified"
    # No session cookie issued under either name (dev or __Host-) — login refused.
    assert "vibony_session" not in r.cookies
    assert "__Host-vibony_session" not in r.cookies


async def test_oidc_email_collision_verified_links(
    http_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    oidc_test_keys: dict,
) -> None:
    await _provision_existing_user(http_client, monkeypatch, "owner@corp.com")

    _oidc_env(monkeypatch)
    _mock_discovery(httpx_mock)
    _mock_jwks(httpx_mock, oidc_test_keys)
    state, nonce, cookies = await _start_flow(http_client)
    _mock_token(
        httpx_mock,
        _make_id_token(
            oidc_test_keys, "owner-sub", "owner@corp.com", nonce=nonce, email_verified=True
        ),
    )
    r = await http_client.get(
        "/auth/oidc/callback",
        params={"code": "x", "state": state},
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 302
    me = await http_client.get("/auth/me", cookies=r.cookies)
    assert me.json()["user"]["email"] == "owner@corp.com"


# ---------------------------------------------------------------------------
# 11. Callback URL requires VIBONY_BASE_URL
# ---------------------------------------------------------------------------

async def test_oidc_start_requires_base_url(
    http_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _oidc_env(monkeypatch)
    monkeypatch.delenv("VIBONY_BASE_URL", raising=False)
    r = await http_client.get("/auth/oidc/start", follow_redirects=False)
    assert r.status_code == 500
    assert r.json()["detail"]["reason"] == "oidc_misconfigured"


# ---------------------------------------------------------------------------
# 12. Upstream discovery failure → 502, not an unhandled 500
# ---------------------------------------------------------------------------

async def test_oidc_discovery_upstream_error_502(
    http_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _oidc_env(monkeypatch)
    httpx_mock.add_response(url=_DISCOVERY_URL, status_code=503)
    r = await http_client.get("/auth/oidc/start", follow_redirects=False)
    assert r.status_code == 502
    assert r.json()["detail"]["reason"] == "oidc_upstream_unavailable"


# ---------------------------------------------------------------------------
# 13. SSRF guard rejects a private-IP issuer when not opted out
# ---------------------------------------------------------------------------

async def test_oidc_ssrf_private_issuer_rejected(
    http_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBONY_AUTH_MODE", "oidc")
    monkeypatch.setenv("VIBONY_OIDC_CLIENT_ID", _CLIENT_ID)
    monkeypatch.setenv("VIBONY_OIDC_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("VIBONY_BASE_URL", "https://app.example.com")
    # Cloud-metadata SSRF target; guard is active (insecure-issuer NOT opted in).
    monkeypatch.setenv("VIBONY_OIDC_ISSUER", "https://169.254.169.254")
    monkeypatch.delenv("VIBONY_OIDC_ALLOW_INSECURE_ISSUER", raising=False)

    r = await http_client.get("/auth/oidc/start", follow_redirects=False)
    assert r.status_code == 500
    assert r.json()["detail"]["reason"] == "oidc_misconfigured"


# ---------------------------------------------------------------------------
# 14. A brand-new subject with no email claim is rejected (not 500)
# ---------------------------------------------------------------------------

async def test_oidc_new_subject_without_email_rejected(
    http_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    oidc_test_keys: dict,
) -> None:
    _oidc_env(monkeypatch)
    _mock_discovery(httpx_mock)
    _mock_jwks(httpx_mock, oidc_test_keys)
    state, nonce, cookies = await _start_flow(http_client)
    # No email claim — a never-seen subject can't be provisioned (users.email NOT NULL).
    _mock_token(httpx_mock, _make_id_token(oidc_test_keys, "sub-no-email", nonce=nonce))

    r = await http_client.get(
        "/auth/oidc/callback",
        params={"code": "x", "state": state},
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "email_required"


# ---------------------------------------------------------------------------
# 15. An ID token with no `exp` is rejected (never-expiring token)
# ---------------------------------------------------------------------------

async def test_oidc_id_token_without_exp_rejected(
    http_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    oidc_test_keys: dict,
) -> None:
    _oidc_env(monkeypatch)
    _mock_discovery(httpx_mock)
    _mock_jwks(httpx_mock, oidc_test_keys)
    state, nonce, cookies = await _start_flow(http_client)

    now = int(time.time())
    # Validly signed token but with NO exp claim — authlib's validate_exp would
    # accept it (only checks an exp that is present); the essential-claim guard
    # must reject it.
    payload = {
        "iss": _ISSUER,
        "sub": "sub-no-exp",
        "aud": _CLIENT_ID,
        "iat": now,
        "email": "noexp@corp.com",
        "nonce": nonce,
    }
    token = jose_jwt.encode({"alg": "RS256", "kid": "test-key-1"}, payload, oidc_test_keys["jwk"])
    token_str = token.decode("utf-8") if isinstance(token, bytes) else token
    _mock_token(httpx_mock, token_str)

    r = await http_client.get(
        "/auth/oidc/callback",
        params={"code": "x", "state": state},
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert r.json()["detail"]["reason"] == "invalid_id_token"
