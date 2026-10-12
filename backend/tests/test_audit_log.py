"""Tests for the workspace audit log."""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")

_ADMIN_EMAIL = "authed_test@example.com"
_ADMIN_PASS = "password123"


async def _audit(client, company_id):
    resp = await client.get(f"/workspaces/{company_id}/audit")
    assert resp.status_code == 200, resp.text
    return resp.json()["entries"]


async def _actions(client, company_id):
    return [e["action"] for e in await _audit(client, company_id)]


async def _add_member(client, company_id, email, role) -> str:
    inv = await client.post(
        f"/workspaces/{company_id}/invites", json={"role": role, "email": email}
    )
    token = inv.json()["token"]
    joined = await client.post(
        f"/invites/{token}/signup-and-accept",
        json={"email": email, "password": "password123"},
    )
    uid = joined.json()["user"]["id"]
    await client.post("/auth/login", json={"email": _ADMIN_EMAIL, "password": _ADMIN_PASS})
    return uid


async def test_session_created_is_audited(authed_client):
    client, company_id = authed_client
    resp = await client.post(
        "/sessions",
        json={"task_id": "task-01-training-loop", "ai_config": {"primary_model": "deepseek/deepseek-chat"}},
    )
    assert resp.status_code == 201, resp.text
    code = resp.json()["session_code"]
    entries = await _audit(client, company_id)
    created = next(e for e in entries if e["action"] == "session.created")
    assert created["target_id"] == code
    assert created["metadata"]["task_id"] == "task-01-training-loop"
    assert created["actor_email"] == _ADMIN_EMAIL


async def test_workspace_created_is_audited(authed_client):
    client, _ = authed_client
    new_id = (await client.post("/workspaces", json={"name": "Audited WS"})).json()["workspace_id"]
    assert "workspace.created" in await _actions(client, new_id)


async def test_invite_and_revoke_audited(authed_client):
    client, company_id = authed_client
    inv = await client.post(
        f"/workspaces/{company_id}/invites", json={"role": "interviewer", "email": "x@e.com"}
    )
    token = inv.json()["token"]
    await client.delete(f"/invites/{token}")
    entries = await _audit(client, company_id)
    actions = [e["action"] for e in entries]
    assert "member.invited" in actions
    assert "invite.revoked" in actions
    # The invited email is resolved into target_email at read time, not
    # frozen into metadata.
    invited = next(e for e in entries if e["action"] == "member.invited")
    assert invited["target_email"] == "x@e.com"
    assert "email" not in invited["metadata"]


async def test_member_role_change_and_remove_audited(authed_client):
    client, company_id = authed_client
    u2 = await _add_member(client, company_id, "m2@example.com", "interviewer")
    await client.patch(f"/workspaces/{company_id}/members/{u2}", json={"role": "admin"})
    await client.delete(f"/workspaces/{company_id}/members/{u2}")
    entries = await _audit(client, company_id)
    actions = [e["action"] for e in entries]
    assert "member.joined" in actions
    assert "member.role_changed" in actions
    assert "member.removed" in actions
    # The removed member's email still resolves at read time (the user row
    # survives a membership removal) — and is not duplicated into metadata.
    removed = next(e for e in entries if e["action"] == "member.removed")
    assert removed["target_email"] == "m2@example.com"
    assert "email" not in removed["metadata"]


async def test_audit_is_most_recent_first(authed_client):
    client, company_id = authed_client
    await client.post(f"/workspaces/{company_id}/invites", json={"role": "interviewer", "email": "a@e.com"})
    await client.post(f"/workspaces/{company_id}/invites", json={"role": "admin", "email": "b@e.com"})
    entries = await _audit(client, company_id)
    times = [e["created_at"] for e in entries]
    assert times == sorted(times, reverse=True)


async def test_audit_requires_admin(authed_client):
    client, company_id = authed_client
    # Second user with no membership in this workspace.
    await client.post("/auth/signup", json={"email": "outsider@e.com", "password": "password123", "company_name": "Other"})
    resp = await client.get(f"/workspaces/{company_id}/audit")
    assert resp.status_code == 403


async def test_audit_404_unknown_workspace(authed_client):
    client, _ = authed_client
    resp = await client.get(f"/workspaces/{uuid.uuid4()}/audit")
    assert resp.status_code in (403, 404)


async def test_audit_404_in_oidc_mode(authed_client, monkeypatch):
    client, company_id = authed_client
    monkeypatch.setenv("VIBONY_AUTH_MODE", "oidc")
    resp = await client.get(f"/workspaces/{company_id}/audit")
    assert resp.status_code == 404
