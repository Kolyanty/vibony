"""Tests for email verification (non-blocking, stateless token)."""

from __future__ import annotations

import uuid

import pytest

from vibony_backend.auth.email import make_email_token, read_email_token

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_signup_starts_unverified(authed_client):
    client, _ = authed_client
    me = (await client.get("/auth/me")).json()
    assert me["user"]["email_verified"] is False


async def test_verify_email_flips_flag(authed_client):
    client, _ = authed_client
    user_id = (await client.get("/auth/me")).json()["user"]["id"]
    token = make_email_token(uuid.UUID(user_id))

    resp = await client.post("/auth/verify-email", json={"token": token})
    assert resp.status_code == 200, resp.text

    me = (await client.get("/auth/me")).json()
    assert me["user"]["email_verified"] is True


async def test_verify_email_is_idempotent(authed_client):
    client, _ = authed_client
    user_id = (await client.get("/auth/me")).json()["user"]["id"]
    token = make_email_token(uuid.UUID(user_id))
    assert (await client.post("/auth/verify-email", json={"token": token})).status_code == 200
    # Second confirm is a no-op 200.
    assert (await client.post("/auth/verify-email", json={"token": token})).status_code == 200


async def test_verify_email_rejects_garbage_token(authed_client):
    client, _ = authed_client
    resp = await client.post("/auth/verify-email", json={"token": "not-a-valid-token"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "invalid_token"


async def test_verify_email_rejects_unknown_user(http_client):
    # Well-formed token for a user that doesn't exist.
    token = make_email_token(uuid.uuid4())
    resp = await http_client.post("/auth/verify-email", json={"token": token})
    assert resp.status_code == 400


async def test_resend_requires_auth(http_client):
    resp = await http_client.post("/auth/verify-email/resend")
    assert resp.status_code == 401


async def test_resend_accepted(authed_client):
    client, _ = authed_client
    resp = await client.post("/auth/verify-email/resend")
    assert resp.status_code == 202


async def test_token_roundtrip(authed_client):
    uid = uuid.uuid4()
    assert read_email_token(make_email_token(uid)) == uid
    assert read_email_token("garbage") is None
