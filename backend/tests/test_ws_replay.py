"""WS replay and interviewer endpoint tests.

Covered:
  - interviewer replay: reconnecting with last_seq receives the missed events
  - interviewer replay: events with seq <= last_seq are not duplicated
  - interviewer hello.last_seq == the current max_seq in the DB
  - interviewer: hello frame with the correct fields
  - interviewer: 4401/4404/4409 close codes
  - interviewer: an inbound frame → error READ_ONLY
  - REST /end on an active session broadcasts session_end over the WS

The candidate CLI WS endpoint was removed along with the CLI runtime; replay
events are seeded straight into the DB via `_seed_events` (sync psycopg — a
separate connection, no clash with the app's asyncpg engine). Replay is read from
the DB, so seeding without the broker is enough; live broadcast is covered via
REST /end.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine, text
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

SESSION_START_PAYLOAD = {
    "task_id": "task-01-training-loop",
    "ai_config": {"model": "test-model"},
    "protocol_version": 1,
    "cli_version": "0.1.0",
}


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_session(client: TestClient) -> dict[str, Any]:
    # POST /sessions requires cookie auth — sign up first (unique email per call).
    client.post("/auth/signup", json={
        "email": f"ws_replay_{uuid.uuid4()}@example.com",
        "password": "password123",
        "company_name": "WS Replay Co",
    })
    resp = client.post("/sessions", json=VALID_PAYLOAD)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _seed_events(database_url: str, code: str, events: list[tuple[str, dict[str, Any]]]) -> int:
    """Insert events straight into the DB and apply the session_start→active /
    session_end→completed status transitions store_event would. Returns the new
    max seq. Uses a sync psycopg engine (own connection — no asyncpg loop clash).
    """
    engine = create_engine(database_url.replace("+asyncpg", "+psycopg"), future=True)
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text("SELECT id, status FROM sessions WHERE code = :code"),
                {"code": code},
            ).one()
            session_id, status = row
            seq = conn.execute(
                text("SELECT COALESCE(MAX(seq), 0) FROM events WHERE session_id = :sid"),
                {"sid": session_id},
            ).scalar() or 0
            for event_type, payload in events:
                seq += 1
                conn.execute(
                    text(
                        "INSERT INTO events (session_id, seq, id, type, client_timestamp, payload) "
                        "VALUES (:sid, :seq, :id, :type, now(), CAST(:payload AS JSONB))"
                    ),
                    {
                        "sid": session_id,
                        "seq": seq,
                        "id": str(uuid.uuid4()),
                        "type": event_type,
                        "payload": json.dumps(payload),
                    },
                )
                if event_type == "session_start" and status == "pending":
                    conn.execute(
                        text("UPDATE sessions SET status = 'active' WHERE id = :sid"),
                        {"sid": session_id},
                    )
                    status = "active"
                elif event_type == "session_end" and status == "active":
                    conn.execute(
                        text(
                            "UPDATE sessions SET status = 'completed', completed_at = now() "
                            "WHERE id = :sid"
                        ),
                        {"sid": session_id},
                    )
                    status = "completed"
            return seq
    finally:
        engine.dispose()


def _interviewer_url(code: str, last_seq: int = 0) -> str:
    return f"/sessions/{code}/interviewer?last_seq={last_seq}"


def _read_until_event_type(ws: Any, event_type: str, cap: int = 10) -> dict[str, Any]:
    """Drain frames (e.g. presence_updated) until an event of the wanted type."""
    for _ in range(cap):
        frame = ws.receive_json()
        if frame.get("kind") == "event" and frame.get("event", {}).get("type") == event_type:
            return frame
    raise AssertionError(f"did not receive event type {event_type!r} within {cap} frames")


# ---------------------------------------------------------------------------
# Tests: replay (driven through the interviewer endpoint)
# ---------------------------------------------------------------------------


def test_hello_last_seq_reflects_db(ws_client: TestClient, database_url: str) -> None:
    """hello.last_seq == 0 for an empty session; after events — the current max."""
    session = _create_session(ws_client)
    code = session["session_code"]

    with ws_client.websocket_connect(_interviewer_url(code)) as ws:
        hello = ws.receive_json()
        assert hello["last_seq"] == 0

    _seed_events(database_url, code, [("session_start", SESSION_START_PAYLOAD)])

    with ws_client.websocket_connect(_interviewer_url(code)) as ws:
        hello2 = ws.receive_json()
        assert hello2["last_seq"] == 1


def test_replay_receives_missed_events(ws_client: TestClient, database_url: str) -> None:
    """Connecting with last_seq=0 receives events 1 and 2 via replay."""
    session = _create_session(ws_client)
    code = session["session_code"]

    _seed_events(database_url, code, [
        ("session_start", SESSION_START_PAYLOAD),
        ("user_message", {"text": "Hi"}),
    ])

    with ws_client.websocket_connect(_interviewer_url(code, last_seq=0)) as ws:
        hello = ws.receive_json()
        assert hello["last_seq"] == 2

        r1 = ws.receive_json()
        assert r1["kind"] == "event"
        assert r1["event"]["seq"] == 1
        assert r1["event"]["type"] == "session_start"

        r2 = ws.receive_json()
        assert r2["kind"] == "event"
        assert r2["event"]["seq"] == 2
        assert r2["event"]["type"] == "user_message"


def test_replay_partial(ws_client: TestClient, database_url: str) -> None:
    """Connecting with last_seq=1 receives only event seq=2."""
    session = _create_session(ws_client)
    code = session["session_code"]

    _seed_events(database_url, code, [
        ("session_start", SESSION_START_PAYLOAD),
        ("user_message", {"text": "Hello"}),
    ])

    with ws_client.websocket_connect(_interviewer_url(code, last_seq=1)) as ws:
        hello = ws.receive_json()
        assert hello["last_seq"] == 2

        r = ws.receive_json()
        assert r["kind"] == "event"
        assert r["event"]["seq"] == 2
        assert r["event"]["type"] == "user_message"


def test_replay_none_when_up_to_date(ws_client: TestClient, database_url: str) -> None:
    """Connecting with last_seq==current_max does not repeat already-seen events.

    After hello the first frame is a presence frame (own-join interviewer), not a
    replay duplicate of seq=1.
    """
    session = _create_session(ws_client)
    code = session["session_code"]

    _seed_events(database_url, code, [("session_start", SESSION_START_PAYLOAD)])

    with ws_client.websocket_connect(_interviewer_url(code, last_seq=1)) as ws:
        hello = ws.receive_json()
        assert hello["last_seq"] == 1

        nxt = ws.receive_json()
        # Whatever comes next, it must NOT be a replay of the already-seen seq=1.
        assert nxt.get("event", {}).get("seq") != 1


# ---------------------------------------------------------------------------
# Tests: interviewer auth
# ---------------------------------------------------------------------------


def test_interviewer_hello_on_fresh_connect(ws_client: TestClient) -> None:
    session = _create_session(ws_client)
    code = session["session_code"]

    with ws_client.websocket_connect(_interviewer_url(code)) as ws:
        hello = ws.receive_json()

    assert hello["kind"] == "hello"
    assert hello["protocol_version"] == PROTOCOL_VERSION
    assert hello["session_status"] == "pending"
    assert hello["last_seq"] == 0


def test_interviewer_no_cookie_closes_4401(ws_client: TestClient) -> None:
    session = _create_session(ws_client)
    code = session["session_code"]
    ws_client.post("/auth/logout")  # clear the session cookie

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with ws_client.websocket_connect(_interviewer_url(code)) as ws:
            ws.receive_json()

    assert exc_info.value.code == 4401


def test_interviewer_session_not_found_closes_4404(ws_client: TestClient) -> None:
    url = "/sessions/ZZZZ-ZZZZ/interviewer"

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with ws_client.websocket_connect(url) as ws:
            ws.receive_json()

    assert exc_info.value.code == 4404


def test_interviewer_completed_session_closes_4409(ws_client: TestClient) -> None:
    session = _create_session(ws_client)
    code = session["session_code"]
    ws_client.post(f"/sessions/{code}/end")  # cookie auth from _create_session signup

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with ws_client.websocket_connect(_interviewer_url(code)) as ws:
            ws.receive_json()

    assert exc_info.value.code == 4409


def test_rest_end_active_session_broadcasts_session_end(
    ws_client: TestClient, database_url: str
) -> None:
    """POST /sessions/{code}/end on an active session broadcasts session_end over the WS.

    Confirms that REST completion publishes the event to the broker and a connected
    client (here — the interviewer) receives it live.
    """
    session = _create_session(ws_client)
    code = session["session_code"]

    # Bring the session to active so /end emits session_end (was_active path).
    _seed_events(database_url, code, [("session_start", SESSION_START_PAYLOAD)])

    with ws_client.websocket_connect(_interviewer_url(code, last_seq=1)) as int_ws:
        int_ws.receive_json()  # hello

        resp = ws_client.post(f"/sessions/{code}/end")
        assert resp.status_code == 200

        end_frame = _read_until_event_type(int_ws, "session_end")
        assert end_frame["event"]["payload"]["reason"] == "interviewer"


def test_rest_end_pending_session_no_event_emitted(ws_client: TestClient) -> None:
    """On a pending session (no session_start) /end does NOT emit a session_end event.

    Otherwise the invariant "session_start is the first event" would be broken.
    """
    session = _create_session(ws_client)
    code = session["session_code"]

    resp = ws_client.post(f"/sessions/{code}/end")  # cookie auth
    assert resp.status_code == 200

    events = ws_client.get(f"/sessions/{code}/events").json()
    assert events == []


# ---------------------------------------------------------------------------
# Tests: interviewer read-only + replay-on-reconnect
# ---------------------------------------------------------------------------


def test_interviewer_send_rejected_with_error(ws_client: TestClient) -> None:
    """Any frame from the interviewer returns error READ_ONLY."""
    session = _create_session(ws_client)
    code = session["session_code"]

    with ws_client.websocket_connect(_interviewer_url(code)) as ws:
        ws.receive_json()  # hello
        ws.receive_json()  # presence_updated direct snapshot (interviewer)
        ws.receive_json()  # broadcast presence_updated (via sub_q)

        ws.send_json({"kind": "event", "event": {"type": "user_message"}})
        resp = ws.receive_json()

    assert resp["kind"] == "error"
    assert resp["code"] == "READ_ONLY"


def test_interviewer_replay_on_reconnect(ws_client: TestClient, database_url: str) -> None:
    """Interviewer with last_seq=0 receives already-recorded events."""
    session = _create_session(ws_client)
    code = session["session_code"]

    _seed_events(database_url, code, [
        ("session_start", SESSION_START_PAYLOAD),
        ("user_message", {"text": "Test"}),
    ])

    with ws_client.websocket_connect(_interviewer_url(code, last_seq=0)) as ws:
        hello = ws.receive_json()
        assert hello["last_seq"] == 2

        r1 = ws.receive_json()
        assert r1["event"]["seq"] == 1

        r2 = ws.receive_json()
        assert r2["event"]["seq"] == 2
