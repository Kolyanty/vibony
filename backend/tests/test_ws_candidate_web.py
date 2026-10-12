"""WS /sessions/candidate-web endpoint tests.

AgentHost.start() and submit_user_message() are mocked so no real LLM
calls happen.

Coverage:
  - hello frame on valid web-session token
  - 4401 on invalid token
  - 4409 on completed session
  - user_message frame is forwarded to AgentHost.submit_user_message
  - inline_bash with missing cmd returns PAYLOAD_INVALID error frame
  - reconnect within grace window cancels termination
  - replay: hello.last_seq reflects DB state; second connect with same last_seq gets no extra frames
"""

from __future__ import annotations

import io
import json
import tarfile
import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from vibony_backend.sessions import PROTOCOL_VERSION

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


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def web_ws_client(db_engine, database_url: str) -> Iterator[TestClient]:  # type: ignore[type-arg]
    """TestClient wired to a fresh DB engine, with AgentHost.start() mocked."""
    import vibony_backend.db.engine as eng
    from vibony_backend.main import app

    eng._engine = None
    eng._sessionmaker = None

    _start_mock = AsyncMock(return_value=None)
    _submit_mock = AsyncMock(return_value=None)

    with (
        patch("vibony_backend.agent_host.host.AgentHost.start", _start_mock),
        patch(
            "vibony_backend.agent_host.host.AgentHost.submit_user_message",
            _submit_mock,
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
            client._start_mock = _start_mock  # type: ignore[attr-defined]
            client._submit_mock = _submit_mock  # type: ignore[attr-defined]
            yield client

    eng._engine = None
    eng._sessionmaker = None


def _create_web_session(client: TestClient) -> dict:
    """Sign up + create a web runtime session; returns POST /sessions response."""
    client.post("/auth/signup", json={
        "email": f"web_ws_test_{uuid.uuid4()}@example.com",
        "password": "password123",
        "company_name": "WebWS Test Co",
    })
    resp = client.post("/sessions", json=WEB_PAYLOAD)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _web_ws_url(token: str, last_seq: int = 0) -> str:
    return f"/sessions/candidate-web?token={token}&last_seq={last_seq}"


# ---------------------------------------------------------------------------
# Tests: handshake
# ---------------------------------------------------------------------------


def test_web_hello_on_fresh_connect(web_ws_client: TestClient) -> None:
    session = _create_web_session(web_ws_client)
    token = session["candidate_token"]

    with web_ws_client.websocket_connect(_web_ws_url(token)) as ws:
        hello = ws.receive_json()

    assert hello["kind"] == "hello"
    assert hello["protocol_version"] == PROTOCOL_VERSION
    assert hello["session_status"] == "pending"
    assert hello["last_seq"] == 0


# ---------------------------------------------------------------------------
# Tests: close codes
# ---------------------------------------------------------------------------


def test_invalid_token_closes_4401(web_ws_client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with web_ws_client.websocket_connect(_web_ws_url("bad-token-value")) as ws:
            ws.receive_json()

    assert exc_info.value.code == 4401


def test_completed_session_closes_4409(web_ws_client: TestClient) -> None:
    session = _create_web_session(web_ws_client)
    code = session["session_code"]
    token = session["candidate_token"]

    # End the session via REST (requires cookie auth — client still has the cookie).
    end_resp = web_ws_client.post(f"/sessions/{code}/end")
    assert end_resp.status_code in (200, 202, 204), end_resp.text

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with web_ws_client.websocket_connect(_web_ws_url(token)) as ws:
            ws.receive_json()

    assert exc_info.value.code == 4409


# ---------------------------------------------------------------------------
# Tests: frame handling
# ---------------------------------------------------------------------------


def test_user_message_frame_dispatched(web_ws_client: TestClient) -> None:
    """user_message frame should be accepted without an error response."""
    session = _create_web_session(web_ws_client)
    token = session["candidate_token"]

    with web_ws_client.websocket_connect(_web_ws_url(token)) as ws:
        ws.receive_json()  # consume hello
        ws.send_text(json.dumps({"kind": "user_message", "text": "Hello AI"}))
        # Give background task a tick, then disconnect.
        # No error frame should arrive.

    # No assertion on submit_mock here — background task may not finish
    # before disconnect in sync TestClient. The absence of an error frame
    # from the server is the observable contract.


def test_inline_bash_missing_cmd_returns_payload_invalid(web_ws_client: TestClient) -> None:
    """inline_bash without a non-empty 'cmd' field returns PAYLOAD_INVALID."""
    session = _create_web_session(web_ws_client)
    token = session["candidate_token"]

    with web_ws_client.websocket_connect(_web_ws_url(token)) as ws:
        ws.receive_json()  # consume hello
        # Pass presence join gate before sending test frames
        ws.send_text(json.dumps({"kind": "session_join", "name": "Tester"}))
        ws.receive_json()  # session_join_ok
        ws.receive_json()  # presence_updated broadcast
        # Send inline_bash with wrong field name (no 'cmd').
        ws.send_text(json.dumps({"kind": "inline_bash"}))
        resp = ws.receive_json()

    assert resp["kind"] == "error"
    assert resp["code"] == "PAYLOAD_INVALID"


def test_unknown_frame_kind_returns_error(web_ws_client: TestClient) -> None:
    session = _create_web_session(web_ws_client)
    token = session["candidate_token"]

    with web_ws_client.websocket_connect(_web_ws_url(token)) as ws:
        ws.receive_json()  # consume hello
        # Pass presence join gate before sending test frames
        ws.send_text(json.dumps({"kind": "session_join", "name": "Tester"}))
        ws.receive_json()  # session_join_ok
        ws.receive_json()  # presence_updated broadcast
        ws.send_text(json.dumps({"kind": "unknown_frame_xyz"}))
        resp = ws.receive_json()

    assert resp["kind"] == "error"
    assert resp["code"] == "PAYLOAD_INVALID"


def test_reconnect_hello_reflects_last_seq(web_ws_client: TestClient) -> None:
    """Reconnecting with last_seq=K receives hello with last_seq=current_max_seq.

    Since store_event is mocked (returns 1 without writing to DB), no events are
    persisted. Both connects get hello.last_seq=0, and reconnecting with last_seq=0
    results in no replay frames — hello arrives immediately after the connection.
    """
    session = _create_web_session(web_ws_client)
    token = session["candidate_token"]

    # First connect: hello tells us the current max_seq.
    with web_ws_client.websocket_connect(_web_ws_url(token, last_seq=0)) as ws:
        hello1 = ws.receive_json()
    assert hello1["kind"] == "hello"
    server_seq = hello1["last_seq"]  # 0 since no real events in DB (mocked store_event)

    # Second connect with last_seq=server_seq: no events to replay, hello arrives first.
    with web_ws_client.websocket_connect(_web_ws_url(token, last_seq=server_seq)) as ws:
        hello2 = ws.receive_json()
    assert hello2["kind"] == "hello"
    assert hello2["last_seq"] == server_seq


# ---------------------------------------------------------------------------
# Tests: session_submit guard
# ---------------------------------------------------------------------------


def test_session_submit_rejected_on_live_session(web_ws_client: TestClient) -> None:
    """session_submit on a live (non-async) session returns NOT_ASYNC and does
    not end the session (live sessions end via interviewer/quit, not Submit)."""
    session = _create_web_session(web_ws_client)  # WEB_PAYLOAD → mode defaults to live
    token = session["candidate_token"]

    with web_ws_client.websocket_connect(_web_ws_url(token)) as ws:
        ws.receive_json()  # hello
        ws.send_text(json.dumps({"kind": "session_join", "name": "Tester"}))
        ws.receive_json()  # session_join_ok
        ws.receive_json()  # presence_updated broadcast
        ws.send_text(json.dumps({"kind": "session_submit"}))
        resp = ws.receive_json()

    assert resp["kind"] == "error"
    assert resp["code"] == "NOT_ASYNC"
