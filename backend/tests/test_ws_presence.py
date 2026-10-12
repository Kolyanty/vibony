"""Tests for session presence (attendance tracking).

Coverage:
  - PresenceRegistry unit tests (join, name_taken, disconnect, get_attendees)
  - Role field in PresenceRecord / get_attendees
  - WS session_join ok flow: session_join_ok + presence_updated broadcast
  - WS join_required gate: non-join frame before session_join returns JOIN_REQUIRED
  - WS session_join_error on invalid name
  - Interviewer WS registers in presence; disconnects on close
"""

from __future__ import annotations

import json
import tarfile
import uuid
from collections.abc import Iterator
from io import BytesIO
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from vibony_backend.sessions.ws import PresenceRegistry


# ---------------------------------------------------------------------------
# PresenceRegistry unit tests
# ---------------------------------------------------------------------------


def test_presence_registry_join_creates_record() -> None:
    reg = PresenceRegistry()
    result = reg.join("sess1", "tok1", "Alice")
    assert not isinstance(result, str)
    assert result.name == "Alice"
    assert result.is_active is True
    assert reg.is_token_active("sess1", "tok1") is True


def test_presence_registry_join_invalid_name_empty() -> None:
    reg = PresenceRegistry()
    assert reg.join("sess1", "tok1", "   ") == "invalid_name"


def test_presence_registry_join_invalid_name_too_long() -> None:
    reg = PresenceRegistry()
    assert reg.join("sess1", "tok1", "A" * 65) == "invalid_name"


def test_presence_registry_name_taken_case_insensitive() -> None:
    reg = PresenceRegistry()
    reg.join("sess1", "tok1", "Alice")
    assert reg.join("sess1", "tok2", "alice") == "name_taken"


def test_presence_registry_name_allowed_after_disconnect() -> None:
    reg = PresenceRegistry()
    reg.join("sess1", "tok1", "Alice")
    reg.disconnect("sess1", "tok1")
    result = reg.join("sess1", "tok2", "Alice")
    assert not isinstance(result, str)
    assert result.is_active is True


def test_presence_registry_reconnect_updates_same_record() -> None:
    reg = PresenceRegistry()
    r1 = reg.join("sess1", "tok1", "Alice")
    assert not isinstance(r1, str)
    reg.disconnect("sess1", "tok1")
    r2 = reg.join("sess1", "tok1", "Alice")
    assert not isinstance(r2, str)
    assert r2 is r1
    assert r2.is_active is True


def test_presence_registry_disconnect_noop_for_unknown_token() -> None:
    reg = PresenceRegistry()
    assert reg.disconnect("sess1", "unknown") is False


def test_presence_registry_duplicate_connection_blocked() -> None:
    reg = PresenceRegistry()
    reg.join("sess1", "tok1", "Alice")
    # Same token still active — second join attempt rejected.
    assert reg.join("sess1", "tok1", "Bob") == "duplicate_connection"
    # Attendees list unchanged.
    attendees = reg.get_attendees("sess1")
    assert len(attendees) == 1
    assert attendees[0]["name"] == "Alice"


def test_presence_registry_rejoin_after_disconnect_succeeds() -> None:
    reg = PresenceRegistry()
    reg.join("sess1", "tok1", "Alice")
    reg.disconnect("sess1", "tok1")
    # Token no longer active — reconnect should succeed.
    result = reg.join("sess1", "tok1", "Alice")
    assert not isinstance(result, str)
    assert result.is_active is True


def test_presence_registry_different_name_same_token_creates_new_record() -> None:
    """When a new person uses the same candidate link (same token) after the first
    person disconnected, a new record is created — old one stays as inactive history."""
    reg = PresenceRegistry()
    r1 = reg.join("sess1", "tok1", "kolyan")
    assert not isinstance(r1, str)
    reg.disconnect("sess1", "tok1")

    # Different name, same token — should create a NEW record.
    r2 = reg.join("sess1", "tok1", "ivan")
    assert not isinstance(r2, str)
    assert r2 is not r1          # new record, not a reuse
    assert r2.name == "ivan"
    assert r2.is_active is True

    # Both records must appear in attendees list.
    attendees = reg.get_attendees("sess1")
    assert len(attendees) == 2
    by_name = {a["name"]: a for a in attendees}
    assert by_name["kolyan"]["is_active"] is False
    assert by_name["ivan"]["is_active"] is True


def test_presence_registry_get_attendees() -> None:
    reg = PresenceRegistry()
    reg.join("sess1", "tok1", "Alice")
    reg.join("sess1", "tok2", "Bob")
    reg.disconnect("sess1", "tok1")
    attendees = reg.get_attendees("sess1")
    assert len(attendees) == 2
    by_name = {a["name"]: a for a in attendees}
    assert by_name["Alice"]["is_active"] is False
    assert by_name["Bob"]["is_active"] is True


# ---------------------------------------------------------------------------
# Role field tests
# ---------------------------------------------------------------------------


def test_presence_registry_default_role_is_candidate() -> None:
    reg = PresenceRegistry()
    result = reg.join("sess1", "tok1", "Alice")
    assert not isinstance(result, str)
    assert result.role == "candidate"


def test_presence_registry_interviewer_role_stored() -> None:
    reg = PresenceRegistry()
    result = reg.join("sess1", "tok1", "interviewer@example.com", role="interviewer")
    assert not isinstance(result, str)
    assert result.role == "interviewer"


def test_presence_registry_get_attendees_includes_role() -> None:
    reg = PresenceRegistry()
    reg.join("sess1", "tok1", "Alice")
    reg.join("sess1", "tok2", "interviewer@example.com", role="interviewer")
    attendees = reg.get_attendees("sess1")
    by_name = {a["name"]: a for a in attendees}
    assert by_name["Alice"]["role"] == "candidate"
    assert by_name["interviewer@example.com"]["role"] == "interviewer"


def test_presence_registry_get_attendees_masked_replaces_interviewer_name() -> None:
    reg = PresenceRegistry()
    reg.join("sess1", "tok1", "alice@example.com", role="interviewer")
    reg.join("sess1", "tok2", "bob@example.com", role="interviewer")
    reg.join("sess1", "tok3", "Candidate")
    masked = reg.get_attendees_masked("sess1")
    by_role_token: dict[str, list[dict]] = {}
    for a in masked:
        by_role_token.setdefault(a["role"], []).append(a)
    # Candidate name unchanged
    assert any(a["name"] == "Candidate" for a in by_role_token["candidate"])
    # First interviewer → "Interviewer", second → "Interviewer 2"
    interviewer_names = {a["name"] for a in by_role_token["interviewer"]}
    assert interviewer_names == {"Interviewer", "Interviewer 2"}
    # No emails leaked
    assert not any("@" in a["name"] for a in masked)


def test_presence_registry_interviewer_disconnect_marks_inactive() -> None:
    reg = PresenceRegistry()
    reg.join("sess1", "tok1", "interviewer@example.com", role="interviewer")
    assert reg.is_token_active("sess1", "tok1") is True
    changed = reg.disconnect("sess1", "tok1")
    assert changed is True
    assert reg.is_token_active("sess1", "tok1") is False
    attendees = reg.get_attendees("sess1")
    assert attendees[0]["is_active"] is False


# ---------------------------------------------------------------------------
# WS integration: session_join flow
# ---------------------------------------------------------------------------


def _make_empty_tgz() -> bytes:
    buf = BytesIO()
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
def presence_ws_client(db_engine, database_url: str) -> Iterator[TestClient]:
    import vibony_backend.db.engine as eng
    from vibony_backend.main import app

    eng._engine = None
    eng._sessionmaker = None

    with (
        patch("vibony_backend.agent_host.host.AgentHost.start", AsyncMock(return_value=None)),
        patch("vibony_backend.agent_host.host.AgentHost.submit_user_message", AsyncMock(return_value=None)),
        patch("vibony_backend.agent_host.host.store_event", new_callable=AsyncMock, return_value=1),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
    ):
        with TestClient(app) as client:
            yield client

    eng._engine = None
    eng._sessionmaker = None


def _create_web_session(client: TestClient) -> dict:
    client.post("/auth/signup", json={
        "email": f"presence_test_{uuid.uuid4()}@example.com",
        "password": "password123",
        "company_name": "Presence Test Co",
    })
    resp = client.post("/sessions", json=WEB_PAYLOAD)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _ws_url(token: str, last_seq: int = 0) -> str:
    return f"/sessions/candidate-web?token={token}&last_seq={last_seq}"


def test_session_join_ok_and_presence_updated(presence_ws_client: TestClient) -> None:
    """session_join with valid name → session_join_ok, then presence_updated broadcast."""
    session = _create_web_session(presence_ws_client)
    token = session["candidate_token"]

    with presence_ws_client.websocket_connect(_ws_url(token)) as ws:
        ws.receive_json()  # hello
        ws.send_text(json.dumps({"kind": "session_join", "name": "Alice"}))
        ok_frame = ws.receive_json()
        updated_frame = ws.receive_json()

    assert ok_frame["kind"] == "session_join_ok"
    assert updated_frame["kind"] == "presence_updated"
    assert len(updated_frame["attendees"]) == 1
    assert updated_frame["attendees"][0]["name"] == "Alice"
    assert updated_frame["attendees"][0]["is_active"] is True


def test_session_join_gate_rejects_other_frames(presence_ws_client: TestClient) -> None:
    """Sending user_message before session_join returns JOIN_REQUIRED error."""
    session = _create_web_session(presence_ws_client)
    token = session["candidate_token"]

    with presence_ws_client.websocket_connect(_ws_url(token)) as ws:
        ws.receive_json()  # hello
        ws.send_text(json.dumps({"kind": "user_message", "text": "hello"}))
        resp = ws.receive_json()

    assert resp["kind"] == "error"
    assert resp["code"] == "JOIN_REQUIRED"


def test_session_join_error_invalid_name(presence_ws_client: TestClient) -> None:
    """session_join with empty name returns session_join_error reason=invalid_name."""
    session = _create_web_session(presence_ws_client)
    token = session["candidate_token"]

    with presence_ws_client.websocket_connect(_ws_url(token)) as ws:
        ws.receive_json()  # hello
        ws.send_text(json.dumps({"kind": "session_join", "name": "   "}))
        resp = ws.receive_json()

    assert resp["kind"] == "session_join_error"
    assert resp["reason"] == "invalid_name"


def test_session_join_retry_after_error(presence_ws_client: TestClient) -> None:
    """After session_join_error, retrying with a valid name succeeds on the same connection."""
    session = _create_web_session(presence_ws_client)
    token = session["candidate_token"]

    with presence_ws_client.websocket_connect(_ws_url(token)) as ws:
        ws.receive_json()  # hello
        # First attempt: invalid name
        ws.send_text(json.dumps({"kind": "session_join", "name": ""}))
        err = ws.receive_json()
        assert err["kind"] == "session_join_error"
        # Retry with valid name
        ws.send_text(json.dumps({"kind": "session_join", "name": "Charlie"}))
        ok = ws.receive_json()
        updated = ws.receive_json()

    assert ok["kind"] == "session_join_ok"
    assert updated["kind"] == "presence_updated"
    assert updated["attendees"][0]["name"] == "Charlie"


def test_session_join_initial_presence_sent_to_interviewer(presence_ws_client: TestClient) -> None:
    """Interviewer connecting after a candidate joins receives presence_updated with current attendees."""
    session = _create_web_session(presence_ws_client)
    token = session["candidate_token"]
    code = session["session_code"]

    # Candidate joins first
    with presence_ws_client.websocket_connect(_ws_url(token)) as ws:
        ws.receive_json()  # hello
        ws.send_text(json.dumps({"kind": "session_join", "name": "Dave"}))
        ws.receive_json()  # session_join_ok
        ws.receive_json()  # presence_updated (Dave joined)

        # Now interviewer connects
        with presence_ws_client.websocket_connect(f"/sessions/{code}/interviewer") as iws:
            iws.receive_json()  # hello
            iws_presence = iws.receive_json()  # direct snapshot includes interviewer + Dave

    assert iws_presence["kind"] == "presence_updated"
    assert any(a["name"] == "Dave" for a in iws_presence["attendees"])


def test_interviewer_ws_registers_in_presence(presence_ws_client: TestClient) -> None:
    """Interviewer WS connection registers the user in presence (role=interviewer)."""
    session = _create_web_session(presence_ws_client)
    code = session["session_code"]

    with presence_ws_client.websocket_connect(f"/sessions/{code}/interviewer") as iws:
        iws.receive_json()  # hello
        snapshot = iws.receive_json()  # direct presence_updated

    assert snapshot["kind"] == "presence_updated"
    assert len(snapshot["attendees"]) == 1
    interviewer = snapshot["attendees"][0]
    assert interviewer["role"] == "interviewer"
    assert interviewer["is_active"] is True


def test_interviewer_ws_disconnect_marks_inactive(presence_ws_client: TestClient) -> None:
    """Interviewer marked inactive in presence after WS disconnect."""
    session = _create_web_session(presence_ws_client)
    code = session["session_code"]
    token = session["candidate_token"]

    # Connect interviewer, then disconnect
    with presence_ws_client.websocket_connect(f"/sessions/{code}/interviewer") as iws:
        iws.receive_json()  # hello
        iws.receive_json()  # presence_updated

    # Candidate connects — should see presence with interviewer as inactive
    with presence_ws_client.websocket_connect(_ws_url(token)) as cws:
        cws.receive_json()  # hello
        cws.send_text(json.dumps({"kind": "session_join", "name": "Tester"}))
        cws.receive_json()  # session_join_ok
        presence = cws.receive_json()  # presence_updated

    interviewer_entry = next(
        (a for a in presence["attendees"] if a["role"] == "interviewer"), None
    )
    assert interviewer_entry is not None
    assert interviewer_entry["is_active"] is False


def test_candidate_sees_interviewer_in_presence(presence_ws_client: TestClient) -> None:
    """Candidate receives presence_updated with interviewer after they join."""
    session = _create_web_session(presence_ws_client)
    code = session["session_code"]
    token = session["candidate_token"]

    with presence_ws_client.websocket_connect(_ws_url(token)) as cws:
        cws.receive_json()  # hello
        cws.send_text(json.dumps({"kind": "session_join", "name": "Candidate"}))
        cws.receive_json()  # session_join_ok
        cws.receive_json()  # presence_updated (only candidate)

        # Interviewer connects — candidate receives broadcast
        with presence_ws_client.websocket_connect(f"/sessions/{code}/interviewer") as iws:
            iws.receive_json()  # hello
            iws.receive_json()  # direct presence snapshot

        # Candidate should have received the broadcast presence_updated
        presence = cws.receive_json()

    assert presence["kind"] == "presence_updated"
    assert any(a["role"] == "interviewer" and a["is_active"] for a in presence["attendees"])


def test_presence_not_leaked_to_unjoined_connection(presence_ws_client: TestClient) -> None:
    """A connection that hasn't completed session_join must not receive
    masked presence frames. The candidate connects but does NOT join; an interviewer
    then joins+leaves (broadcasting presence). Because the candidate's presence queue
    is registered only after a successful join, the candidate's very first frame after
    it finally joins is session_join_ok — no leaked presence_updated was queued."""
    session = _create_web_session(presence_ws_client)
    code = session["session_code"]
    token = session["candidate_token"]

    with presence_ws_client.websocket_connect(_ws_url(token)) as cws:
        cws.receive_json()  # hello — candidate has NOT joined yet

        # An interviewer joins and leaves while the candidate is un-joined; each
        # transition broadcasts presence_updated to the *joined* candidate queues.
        with presence_ws_client.websocket_connect(f"/sessions/{code}/interviewer") as iws:
            iws.receive_json()  # hello
            iws.receive_json()  # direct presence snapshot

        # Now the candidate joins. With the bug, a leaked presence_updated would sit
        # ahead of session_join_ok in the queue; with the fix it does not exist.
        cws.send_text(json.dumps({"kind": "session_join", "name": "Eve"}))
        first = cws.receive_json()
        assert first["kind"] == "session_join_ok", first
        second = cws.receive_json()
        assert second["kind"] == "presence_updated", second
        assert any(a["name"] == "Eve" for a in second["attendees"])


# ---------------------------------------------------------------------------
# Interviewer email privacy
# ---------------------------------------------------------------------------


def test_candidate_sees_masked_interviewer_name(presence_ws_client: TestClient) -> None:
    """Candidate receives 'Interviewer' in presence_updated, not the real email."""
    session = _create_web_session(presence_ws_client)
    code = session["session_code"]
    token = session["candidate_token"]

    with presence_ws_client.websocket_connect(_ws_url(token)) as cws:
        cws.receive_json()  # hello
        cws.send_text(json.dumps({"kind": "session_join", "name": "Candidate"}))
        cws.receive_json()  # session_join_ok
        cws.receive_json()  # presence_updated (only candidate)

        with presence_ws_client.websocket_connect(f"/sessions/{code}/interviewer") as iws:
            iws.receive_json()  # hello
            iws.receive_json()  # direct presence snapshot

        presence = cws.receive_json()

    assert presence["kind"] == "presence_updated"
    interviewer = next((a for a in presence["attendees"] if a["role"] == "interviewer"), None)
    assert interviewer is not None
    assert interviewer["name"] == "Interviewer"
    assert "@" not in interviewer["name"]


def test_interviewer_sees_real_name_in_presence(presence_ws_client: TestClient) -> None:
    """Interviewer direct snapshot contains the real email, not a masked name."""
    session = _create_web_session(presence_ws_client)
    code = session["session_code"]

    with presence_ws_client.websocket_connect(f"/sessions/{code}/interviewer") as iws:
        iws.receive_json()  # hello
        snapshot = iws.receive_json()  # direct presence_updated with full names

    interviewer = next((a for a in snapshot["attendees"] if a["role"] == "interviewer"), None)
    assert interviewer is not None
    assert "@" in interviewer["name"]
