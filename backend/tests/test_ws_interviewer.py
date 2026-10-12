"""WS interviewer endpoint cookie-auth tests.

Covers:
  - no cookie → 4401
  - cross-tenant cookie (user not a member of session's company) → 4403
  - same-tenant cookie → connects (hello frame)
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from vibony_backend.sessions import PROTOCOL_VERSION

VALID_PAYLOAD = {
    "task_id": "task-01-training-loop",
    "ai_config": {
        "primary_model": "deepseek/deepseek-chat",
        "fallback_models": [],
    },
}


@pytest.fixture
def ws_client(db_engine, database_url: str) -> Iterator[TestClient]:  # type: ignore[type-arg]
    import vibony_backend.db.engine as eng
    from vibony_backend.main import app

    eng._engine = None
    eng._sessionmaker = None

    with TestClient(app) as client:
        yield client

    eng._engine = None
    eng._sessionmaker = None


def _create_session(client: TestClient, suffix: str = "") -> dict:
    client.post("/auth/signup", json={
        "email": f"int_test_{suffix}_{uuid.uuid4()}@example.com",
        "password": "password123",
        "company_name": f"Int Co {suffix}",
    })
    resp = client.post("/sessions", json=VALID_PAYLOAD)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _interviewer_url(code: str) -> str:
    return f"/sessions/{code}/interviewer"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_interviewer_no_cookie_closes_4401(ws_client: TestClient) -> None:
    session = _create_session(ws_client, "nocookie")
    code = session["session_code"]
    ws_client.post("/auth/logout")  # clear session cookie

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with ws_client.websocket_connect(_interviewer_url(code)) as ws:
            ws.receive_json()

    assert exc_info.value.code == 4401


def test_interviewer_cross_tenant_closes_4403(ws_client: TestClient) -> None:
    # Create session as user A (company A) — cookie set to user A
    session_a = _create_session(ws_client, "a")
    code_a = session_a["session_code"]

    # Sign up user B in a different company — cookie switches to user B
    ws_client.post("/auth/signup", json={
        "email": f"int_b_{uuid.uuid4()}@example.com",
        "password": "password123",
        "company_name": "Company B cross-tenant",
    })

    # User B tries to access company A's session → 4403
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with ws_client.websocket_connect(_interviewer_url(code_a)) as ws:
            ws.receive_json()

    assert exc_info.value.code == 4403


def test_interviewer_same_tenant_connects(ws_client: TestClient) -> None:
    session = _create_session(ws_client, "sametenant")
    code = session["session_code"]

    with ws_client.websocket_connect(_interviewer_url(code)) as ws:
        hello = ws.receive_json()

    assert hello["kind"] == "hello"
    assert hello["protocol_version"] == PROTOCOL_VERSION
    assert hello["session_status"] == "pending"
    assert hello["last_seq"] == 0
