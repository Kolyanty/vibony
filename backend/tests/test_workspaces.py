"""Tests for workspace creation + plan/trial stub + workspace deletion."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vibony_backend.db.models import Company, Invite, Membership

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_signup_company_is_on_trial(authed_client):
    """A bootstrap company from signup carries plan='trial' + a trial_ends_at."""
    client, company_id = authed_client
    me = (await client.get("/auth/me")).json()
    m = next(x for x in me["memberships"] if x["workspace_id"] == company_id)
    assert m["plan"] == "trial"
    assert m["trial_ends_at"] is not None
    assert m["role"] == "admin"


async def test_create_workspace_makes_admin_membership(authed_client):
    client, company_id = authed_client
    resp = await client.post("/workspaces", json={"name": "Second Workspace"})
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["company_name"] == "Second Workspace"
    assert data["role"] == "admin"
    assert data["plan"] == "trial"
    assert data["trial_ends_at"] is not None
    new_id = data["workspace_id"]
    assert new_id != company_id

    # Shows up as a second membership in /me.
    me = (await client.get("/auth/me")).json()
    ids = {m["workspace_id"] for m in me["memberships"]}
    assert {company_id, new_id} <= ids


async def test_create_workspace_requires_auth(http_client):
    resp = await http_client.post("/workspaces", json={"name": "Nope"})
    assert resp.status_code == 401


async def test_create_workspace_rejects_empty_name(authed_client):
    client, _ = authed_client
    resp = await client.post("/workspaces", json={"name": ""})
    assert resp.status_code == 422


async def test_create_workspace_rejects_whitespace_name(authed_client):
    """A whitespace-only name passes min_length but must not be stored."""
    client, _ = authed_client
    resp = await client.post("/workspaces", json={"name": "   "})
    assert resp.status_code == 422


async def test_create_workspace_strips_name(authed_client):
    """Surrounding whitespace is stripped before storage."""
    client, _ = authed_client
    resp = await client.post("/workspaces", json={"name": "  Padded WS  "})
    assert resp.status_code == 201, resp.text
    assert resp.json()["company_name"] == "Padded WS"


async def test_create_workspace_404_in_oidc_mode(authed_client, monkeypatch):
    client, _ = authed_client
    monkeypatch.setenv("VIBONY_AUTH_MODE", "oidc")
    resp = await client.post("/workspaces", json={"name": "X"})
    assert resp.status_code == 404


# -------- DELETE /workspaces/{id} --------


async def test_delete_workspace_removes_it(authed_client):
    """DELETE a secondary workspace → 204, and it drops out of /me."""
    client, company_id = authed_client
    new_id = (
        await client.post("/workspaces", json={"name": "Doomed"})
    ).json()["workspace_id"]

    resp = await client.delete(f"/workspaces/{new_id}")
    assert resp.status_code == 204, resp.text

    me = (await client.get("/auth/me")).json()
    ids = {m["workspace_id"] for m in me["memberships"]}
    assert new_id not in ids
    assert company_id in ids  # the bootstrap workspace is untouched


async def test_delete_workspace_cascades(authed_client, db_engine):
    """The company row + its memberships + invites are hard-deleted (DB cascade)."""
    client, _ = authed_client
    new_id = (
        await client.post("/workspaces", json={"name": "Cascade WS"})
    ).json()["workspace_id"]
    # Plant an invite so we can prove the cascade reaches a child row.
    inv = await client.post(
        f"/workspaces/{new_id}/invites",
        json={"role": "interviewer", "email": "cascade@e.com"},
    )
    assert inv.status_code == 201, inv.text

    resp = await client.delete(f"/workspaces/{new_id}")
    assert resp.status_code == 204, resp.text

    cid = uuid.UUID(new_id)
    sm = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    async with sm() as db:
        assert (
            await db.execute(select(Company).where(Company.id == cid))
        ).scalar_one_or_none() is None
        assert (
            await db.execute(select(Membership).where(Membership.company_id == cid))
        ).first() is None
        assert (
            await db.execute(select(Invite).where(Invite.company_id == cid))
        ).first() is None


async def test_delete_workspace_allows_last_one(authed_client):
    """Deleting the only workspace you belong to is allowed → zero memberships after."""
    client, company_id = authed_client
    resp = await client.delete(f"/workspaces/{company_id}")
    assert resp.status_code == 204, resp.text
    me = (await client.get("/auth/me")).json()
    assert me["memberships"] == []


async def test_delete_workspace_requires_admin(authed_client):
    """A caller with no membership in the workspace can't delete it (403)."""
    client, company_id = authed_client
    # Switch to an outsider account (signup logs them in, no membership here).
    await client.post(
        "/auth/signup",
        json={
            "email": "ws_delete_outsider@e.com",
            "password": "password123",
            "company_name": "Other",
        },
    )
    resp = await client.delete(f"/workspaces/{company_id}")
    assert resp.status_code == 403


async def test_delete_workspace_requires_auth(authed_client):
    client, company_id = authed_client
    await client.post("/auth/logout")
    resp = await client.delete(f"/workspaces/{company_id}")
    assert resp.status_code == 401


async def test_delete_workspace_404_in_oidc_mode(authed_client, monkeypatch):
    client, company_id = authed_client
    monkeypatch.setenv("VIBONY_AUTH_MODE", "oidc")
    resp = await client.delete(f"/workspaces/{company_id}")
    assert resp.status_code == 404
