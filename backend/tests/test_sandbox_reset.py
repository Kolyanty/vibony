"""Tests for POST /sessions/{code}/sandbox/reset.

Covers:
  - 400 when session is already completed
  - 404 when no active AgentHost is registered
  - 200 + host.reset_sandbox() called on success
"""

from __future__ import annotations

import io
import tarfile
import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_empty_tgz() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz"):
        pass
    return buf.getvalue()


WEB_PAYLOAD = {
    "task_id": "task-01-training-loop",
    "ai_config": {
        "primary_model": "deepseek/deepseek-chat",
        "fallback_models": [],
    },
}


@pytest.fixture
def reset_client(db_engine, database_url: str) -> Iterator[TestClient]:  # type: ignore[type-arg]
    """TestClient wired to a fresh DB with AgentHost.start mocked."""
    import vibony_backend.db.engine as eng
    from vibony_backend.main import app

    eng._engine = None
    eng._sessionmaker = None

    with (
        patch("vibony_backend.agent_host.host.AgentHost.start", AsyncMock(return_value=None)),
        patch(
            "vibony_backend.agent_host.host.AgentHost.submit_user_message",
            AsyncMock(return_value=None),
        ),
        patch(
            "vibony_backend.agent_host.host.store_event",
            new_callable=AsyncMock,
            return_value=1,
        ),
        patch(
            "vibony_backend.agent_host.host._load_loaded_task",
            return_value=None,
        ),
    ):
        with TestClient(app) as client:
            yield client

    eng._engine = None
    eng._sessionmaker = None


def _signup_and_create(client: TestClient, payload: dict) -> dict:
    client.post("/auth/signup", json={
        "email": f"reset_test_{uuid.uuid4()}@example.com",
        "password": "password123",
        "company_name": "Reset Test Co",
    })
    resp = client.post("/sessions", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_reset_sandbox_completed_session_returns_410(reset_client: TestClient) -> None:
    """POST /sessions/{code}/sandbox/reset on a completed session returns 410.

    Completed-state rejections are standardized on 410 Gone (was 400 here),
    matching the bundle / get_session / replay gates; reason ``completed``.
    """
    session = _signup_and_create(reset_client, WEB_PAYLOAD)
    code = session["session_code"]

    end_resp = reset_client.post(f"/sessions/{code}/end")
    assert end_resp.status_code == 200

    resp = reset_client.post(f"/sessions/{code}/sandbox/reset")
    assert resp.status_code == 410
    assert resp.json()["detail"]["reason"] == "completed"


def test_reset_sandbox_no_active_host_returns_404(reset_client: TestClient) -> None:
    """POST /sessions/{code}/sandbox/reset when candidate hasn't connected returns 404."""
    session = _signup_and_create(reset_client, WEB_PAYLOAD)
    code = session["session_code"]

    # No WS connect → no AgentHost in registry → 404.
    resp = reset_client.post(f"/sessions/{code}/sandbox/reset")
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason"] == "sandbox_not_found"


def test_reset_sandbox_calls_host_reset(reset_client: TestClient) -> None:
    """POST /sessions/{code}/sandbox/reset calls AgentHost.reset_sandbox and returns 200."""
    from vibony_backend.agent_host.host import agent_host_registry

    session = _signup_and_create(reset_client, WEB_PAYLOAD)
    code = session["session_code"]
    token = session["candidate_token"]

    # Inject a fake host into the registry so the endpoint finds one.
    fake_host = MagicMock()
    fake_host.reset_sandbox = AsyncMock(return_value=None)
    agent_host_registry._hosts[code] = fake_host

    try:
        resp = reset_client.post(f"/sessions/{code}/sandbox/reset")
    finally:
        agent_host_registry._hosts.pop(code, None)

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    fake_host.reset_sandbox.assert_awaited_once()
