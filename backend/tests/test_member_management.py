"""Tests for member role change + removal (last-admin protected)."""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")

_ADMIN_EMAIL = "authed_test@example.com"
_ADMIN_PASS = "password123"


async def _add_member(client, company_id, email, role) -> str:
    """Invite + signup-and-accept a new member of ``company_id``; return their user id.

    Leaves the client logged back in as the original admin.
    """
    inv = await client.post(
        f"/workspaces/{company_id}/invites", json={"role": role, "email": email}
    )
    assert inv.status_code == 201, inv.text
    token = inv.json()["token"]
    joined = await client.post(
        f"/invites/{token}/signup-and-accept",
        json={"email": email, "password": "password123"},
    )
    assert joined.status_code == 200, joined.text
    new_user_id = joined.json()["user"]["id"]
    # signup-and-accept swapped the session cookie to the new user — restore admin.
    relog = await client.post(
        "/auth/login", json={"email": _ADMIN_EMAIL, "password": _ADMIN_PASS}
    )
    assert relog.status_code == 200
    return new_user_id


async def test_admin_changes_member_role(authed_client):
    client, company_id = authed_client
    u2 = await _add_member(client, company_id, "m2@example.com", "interviewer")

    resp = await client.patch(
        f"/workspaces/{company_id}/members/{u2}", json={"role": "admin"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "admin"

    members = (await client.get(f"/workspaces/{company_id}/members")).json()["members"]
    assert next(m for m in members if m["user_id"] == u2)["role"] == "admin"


async def test_non_admin_cannot_change_role(authed_client):
    client, company_id = authed_client
    u2 = await _add_member(client, company_id, "m2@example.com", "interviewer")

    await client.post("/auth/login", json={"email": "m2@example.com", "password": "password123"})
    resp = await client.patch(
        f"/workspaces/{company_id}/members/{u2}", json={"role": "admin"}
    )
    assert resp.status_code == 403


async def test_cannot_demote_last_admin(authed_client):
    client, company_id = authed_client
    me = (await client.get("/auth/me")).json()
    u1 = me["user"]["id"]
    resp = await client.patch(
        f"/workspaces/{company_id}/members/{u1}", json={"role": "interviewer"}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "last_admin"


async def test_can_demote_admin_when_another_exists(authed_client):
    client, company_id = authed_client
    me = (await client.get("/auth/me")).json()
    u1 = me["user"]["id"]
    await _add_member(client, company_id, "m2@example.com", "admin")

    resp = await client.patch(
        f"/workspaces/{company_id}/members/{u1}", json={"role": "interviewer"}
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "interviewer"


async def test_remove_member(authed_client):
    client, company_id = authed_client
    u2 = await _add_member(client, company_id, "m2@example.com", "interviewer")

    resp = await client.delete(f"/workspaces/{company_id}/members/{u2}")
    assert resp.status_code == 204

    members = (await client.get(f"/workspaces/{company_id}/members")).json()["members"]
    assert all(m["user_id"] != u2 for m in members)


async def test_cannot_remove_last_admin(authed_client):
    client, company_id = authed_client
    u1 = (await client.get("/auth/me")).json()["user"]["id"]
    resp = await client.delete(f"/workspaces/{company_id}/members/{u1}")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "last_admin"


async def test_remove_unknown_member_404(authed_client):
    client, company_id = authed_client
    resp = await client.delete(f"/workspaces/{company_id}/members/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_member_endpoints_404_in_oidc_mode(authed_client, monkeypatch):
    client, company_id = authed_client
    u1 = (await client.get("/auth/me")).json()["user"]["id"]
    monkeypatch.setenv("VIBONY_AUTH_MODE", "oidc")
    assert (
        await client.patch(
            f"/workspaces/{company_id}/members/{u1}", json={"role": "interviewer"}
        )
    ).status_code == 404
    assert (
        await client.delete(f"/workspaces/{company_id}/members/{u1}")
    ).status_code == 404
