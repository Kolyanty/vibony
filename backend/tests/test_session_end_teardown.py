"""Tests for sandbox force-terminate on session end.

The End session handler (REST POST /sessions/{code}/end) must immediately tear
down the web-mode AgentHost / sandbox via AgentHostRegistry.force_terminate,
instead of leaving the container alive until the disconnect grace timer fires.

Covers:
  - force_terminate runs once and removes the host after End session (web).
  - no-op (no error) when no host is registered (CLI session / candidate gone).
  - idempotent across a second End session call.
"""

from __future__ import annotations

import io
import tarfile
import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient


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
def end_client(db_engine, database_url: str) -> Iterator[TestClient]:  # type: ignore[type-arg]
    """TestClient wired to a fresh DB with AgentHost provisioning mocked out."""
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
    signup = client.post("/auth/signup", json={
        "email": f"end_test_{uuid.uuid4()}@example.com",
        "password": "password123",
        "company_name": "End Test Co",
    })
    assert signup.status_code in (200, 201), signup.text
    resp = client.post("/sessions", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_end_session_force_terminates_host(end_client: TestClient) -> None:
    """End session tears down the registered web AgentHost exactly once."""
    from vibony_backend.agent_host.host import agent_host_registry

    session = _signup_and_create(end_client, WEB_PAYLOAD)
    code = session["session_code"]

    fake_host = MagicMock()
    fake_host.terminate = AsyncMock(return_value=None)
    fake_host.quiesce = AsyncMock(return_value=None)  # drain the in-flight turn
    agent_host_registry._hosts[code] = fake_host

    try:
        resp = end_client.post(f"/sessions/{code}/end")
    finally:
        agent_host_registry._hosts.pop(code, None)

    assert resp.status_code == 200, resp.text
    fake_host.terminate.assert_awaited_once()
    # Host removed from the registry by force_terminate.
    assert agent_host_registry.get(code) is None


def test_end_session_no_host_is_noop(end_client: TestClient) -> None:
    """End session on a session with no registered host succeeds (candidate never connected)."""
    from vibony_backend.agent_host.host import agent_host_registry

    session = _signup_and_create(end_client, WEB_PAYLOAD)
    code = session["session_code"]
    assert agent_host_registry.get(code) is None

    resp = end_client.post(f"/sessions/{code}/end")
    assert resp.status_code == 200, resp.text
    assert agent_host_registry.get(code) is None


def test_end_session_force_terminate_idempotent(end_client: TestClient) -> None:
    """A second End session call still succeeds and does not re-terminate a gone host."""
    from vibony_backend.agent_host.host import agent_host_registry

    session = _signup_and_create(end_client, WEB_PAYLOAD)
    code = session["session_code"]

    fake_host = MagicMock()
    fake_host.terminate = AsyncMock(return_value=None)
    fake_host.quiesce = AsyncMock(return_value=None)  # drain the in-flight turn
    agent_host_registry._hosts[code] = fake_host

    try:
        first = end_client.post(f"/sessions/{code}/end")
        second = end_client.post(f"/sessions/{code}/end")
    finally:
        agent_host_registry._hosts.pop(code, None)

    assert first.status_code == 200
    assert second.status_code == 200
    # Terminate ran only on the first call; the second found no host.
    fake_host.terminate.assert_awaited_once()
