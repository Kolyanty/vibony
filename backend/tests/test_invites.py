"""Tests for the invite flow and team member endpoints."""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")


# -------- helpers --------


async def _signup_second_user(http_client, email="user2@example.com"):
    """Create a second user account and return (client, response JSON)."""
    resp = await http_client.post("/auth/signup", json={
        "email": email,
        "password": "password123",
        "company_name": "Second Co",
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


# -------- create invite (admin) --------


async def test_create_invite_admin(authed_client):
    client, company_id = authed_client
    resp = await client.post(
        f"/workspaces/{company_id}/invites",
        json={"role": "interviewer", "email": "invited@example.com"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["role"] == "interviewer"
    assert data["email"] == "invited@example.com"
    assert data["status"] == "pending"
    assert "token" in data
    assert "accept_url" in data
    assert data["token"] in data["accept_url"]


# -------- non-admin → 403 --------


async def test_create_invite_non_admin(authed_client):
    client, company_id = authed_client
    # Second user gets their own company but no membership in Test Co.
    await _signup_second_user(client)
    resp = await client.post("/auth/login", json={"email": "user2@example.com", "password": "password123"})
    assert resp.status_code == 200

    # Verify the invariant explicitly: second user has no Test Co membership.
    me_resp = await client.get("/auth/me")
    assert me_resp.status_code == 200
    me_company_ids = [m["workspace_id"] for m in me_resp.json()["memberships"]]
    assert company_id not in me_company_ids, "test invariant broken: second user must not be in Test Co"

    resp = await client.post(
        f"/workspaces/{company_id}/invites",
        json={"role": "interviewer"},
    )
    assert resp.status_code == 403


# -------- accept invite → membership created --------


async def test_accept_invite(authed_client):
    client, company_id = authed_client

    # Create invite as admin
    resp = await client.post(
        f"/workspaces/{company_id}/invites",
        json={"role": "interviewer"},
    )
    assert resp.status_code == 201
    token = resp.json()["token"]

    # Sign up second user
    await _signup_second_user(client, email="acceptor@example.com")
    resp = await client.post("/auth/login", json={"email": "acceptor@example.com", "password": "password123"})
    assert resp.status_code == 200

    # Accept invite
    resp = await client.post(f"/invites/{token}/accept")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # Re-login as admin to verify membership via /auth/me memberships would include Test Co
    resp = await client.post("/auth/login", json={"email": "authed_test@example.com", "password": "password123"})
    assert resp.status_code == 200

    members = await client.get(f"/workspaces/{company_id}/members")
    assert members.status_code == 200
    emails = [m["email"] for m in members.json()["members"]]
    assert "acceptor@example.com" in emails


# -------- double-accept → 410 --------


async def test_double_accept(authed_client):
    client, company_id = authed_client

    resp = await client.post(
        f"/workspaces/{company_id}/invites",
        json={"role": "interviewer"},
    )
    assert resp.status_code == 201
    token = resp.json()["token"]

    await _signup_second_user(client, email="doublecceptor@example.com")
    await client.post("/auth/login", json={"email": "doublecceptor@example.com", "password": "password123"})

    assert (await client.post(f"/invites/{token}/accept")).status_code == 200
    resp = await client.post(f"/invites/{token}/accept")
    assert resp.status_code == 410
    assert resp.json()["detail"]["reason"] == "accepted"


# -------- revoke → 410 on accept --------


async def test_revoke_then_accept(authed_client):
    client, company_id = authed_client

    resp = await client.post(
        f"/workspaces/{company_id}/invites",
        json={"role": "interviewer"},
    )
    assert resp.status_code == 201
    token = resp.json()["token"]

    # Admin revokes
    del_resp = await client.delete(f"/invites/{token}")
    assert del_resp.status_code == 204

    # Invite should appear as revoked in list
    list_resp = await client.get(f"/workspaces/{company_id}/invites")
    assert list_resp.status_code == 200
    statuses = [inv["status"] for inv in list_resp.json()]
    assert "revoked" in statuses

    # Accept attempt → 410
    await _signup_second_user(client, email="revoketest@example.com")
    await client.post("/auth/login", json={"email": "revoketest@example.com", "password": "password123"})
    resp = await client.post(f"/invites/{token}/accept")
    assert resp.status_code == 410
    assert resp.json()["detail"]["reason"] == "revoked"


# -------- preview without auth --------


async def test_preview_no_auth(authed_client):
    client, company_id = authed_client

    resp = await client.post(
        f"/workspaces/{company_id}/invites",
        json={"role": "admin", "email": "preview@example.com"},
    )
    assert resp.status_code == 201
    token = resp.json()["token"]

    # Clear cookie (simulate unauthenticated)
    client.cookies.clear()

    preview = await client.get(f"/invites/{token}")
    assert preview.status_code == 200
    data = preview.json()
    assert data["role"] == "admin"
    assert data["company_name"] == "Test Co"
    assert data["status"] == "pending"
    # inviter_email is masked (PII protection on the unauthenticated endpoint)
    assert "inviter_email" in data
    assert data["inviter_email"].startswith("a")
    assert "***@" in data["inviter_email"]

    # Re-auth for any subsequent tests that share the client
    await client.post("/auth/login", json={"email": "authed_test@example.com", "password": "password123"})


# -------- admin invite must be email-bound --------


async def test_create_admin_invite_without_email_rejected(authed_client):
    """An emailless admin invite is a self-service admin grant for any holder."""
    client, company_id = authed_client
    resp = await client.post(
        f"/workspaces/{company_id}/invites",
        json={"role": "admin"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "admin_invite_requires_email"


async def test_create_admin_invite_with_email_ok(authed_client):
    client, company_id = authed_client
    resp = await client.post(
        f"/workspaces/{company_id}/invites",
        json={"role": "admin", "email": "bound_admin@example.com"},
    )
    assert resp.status_code == 201


async def test_create_interviewer_invite_without_email_ok(authed_client):
    """Emailless interviewer invites stay allowed — only admin grants are bound."""
    client, company_id = authed_client
    resp = await client.post(
        f"/workspaces/{company_id}/invites",
        json={"role": "interviewer"},
    )
    assert resp.status_code == 201


# -------- re-validate inviter authority at accept --------


async def test_accept_rejected_when_inviter_lost_admin(authed_client):
    """An invite is the inviter's authority; if the inviter is demoted before
    accept, the snapshotted role must not still be granted."""
    client, company_id = authed_client

    # A (bootstrap admin) invites B as admin so the workspace keeps a second admin
    # after A is demoted, then invites C (interviewer) — A is the inviter of both.
    token_b = (
        await client.post(
            f"/workspaces/{company_id}/invites",
            json={"role": "admin", "email": "r50_b@example.com"},
        )
    ).json()["token"]
    token_c = (
        await client.post(
            f"/workspaces/{company_id}/invites",
            json={"role": "interviewer", "email": "r50_c@example.com"},
        )
    ).json()["token"]

    # B signs up + accepts (A still admin → allowed) → B is now an admin.
    client.cookies.clear()
    resp = await client.post(
        f"/invites/{token_b}/signup-and-accept",
        json={"email": "r50_b@example.com", "password": "password123"},
    )
    assert resp.status_code == 200

    # B demotes A to interviewer.
    members = (await client.get(f"/workspaces/{company_id}/members")).json()["members"]
    a_id = next(m["user_id"] for m in members if m["email"] == "authed_test@example.com")
    patch = await client.patch(
        f"/workspaces/{company_id}/members/{a_id}", json={"role": "interviewer"}
    )
    assert patch.status_code == 200

    # C tries to accept A's (now-unauthorized) invite → 410 inviter_unauthorized.
    client.cookies.clear()
    resp = await client.post(
        f"/invites/{token_c}/signup-and-accept",
        json={"email": "r50_c@example.com", "password": "password123"},
    )
    assert resp.status_code == 410
    assert resp.json()["detail"]["reason"] == "inviter_unauthorized"


# -------- oidc mode → 404 --------


async def test_invite_routes_oidc_mode(authed_client, monkeypatch):
    client, company_id = authed_client
    monkeypatch.setenv("VIBONY_AUTH_MODE", "oidc")
    try:
        fake_token = str(uuid.uuid4())
        resp = await client.get(f"/invites/{fake_token}")
        assert resp.status_code == 404

        resp = await client.post(
            f"/workspaces/{company_id}/invites",
            json={"role": "interviewer"},
        )
        assert resp.status_code == 404
    finally:
        monkeypatch.setenv("VIBONY_AUTH_MODE", "password")
