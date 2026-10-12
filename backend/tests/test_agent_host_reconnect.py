"""Tests for AgentHost reconnect / grace-timer behaviour and message
persistence across cold-starts.

All tests are pure asyncio — no DB required. store_event and other I/O
are mocked so we can exercise the registry grace logic in isolation.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest



# ---------------------------------------------------------------------------
# Helpers (duplicated from test_agent_host.py to avoid shared state)
# ---------------------------------------------------------------------------


def _make_empty_tgz() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz"):
        pass
    return buf.getvalue()


def _make_host(**kwargs):
    from vibony_backend.agent_host.host import AgentHost

    return AgentHost(
        session_id=kwargs.get("session_id", uuid.uuid4()),
        session_code=kwargs.get("session_code", "TEST-AAAA"),
        task_id=kwargs.get("task_id", "task-01-training-loop"),
        ai_config=kwargs.get(
            "ai_config",
            {"primary_model": "test-model"},
        ),
    )


# ---------------------------------------------------------------------------
# Tests: registry grace-timer behaviour
# ---------------------------------------------------------------------------


async def test_disconnect_schedules_terminate():
    """connection_closed (last connection) creates a pending grace task."""
    from vibony_backend.agent_host.host import AgentHostRegistry

    reg = AgentHostRegistry()
    fake_host = MagicMock()
    fake_host.terminate = AsyncMock()
    reg._hosts["DISC-0001"] = fake_host
    reg._connect_epochs["DISC-0001"] = 1

    await reg.connection_closed("DISC-0001", 1)

    assert "DISC-0001" in reg._grace_tasks
    task = reg._grace_tasks["DISC-0001"]
    assert not task.done()

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_reconnect_within_grace_cancels_terminate():
    """get_or_create within the grace window must cancel the pending timer."""
    from vibony_backend.agent_host import host as host_module
    from vibony_backend.agent_host.host import AgentHostRegistry

    original = host_module.DISCONNECT_GRACE_SECONDS
    host_module.DISCONNECT_GRACE_SECONDS = 10  # long enough to not fire

    reg = AgentHostRegistry()
    session_id = uuid.uuid4()
    ai_config = {"primary_model": "test-model"}

    host, _, epoch = await reg.get_or_create(
        "RCON-0001", session_id, "task-01-training-loop", ai_config
    )
    await reg.connection_closed("RCON-0001", epoch)

    # Reconnect: get_or_create should cancel the timer.
    host2, was_created, _ = await reg.get_or_create(
        "RCON-0001", session_id, "task-01-training-loop", ai_config
    )

    assert was_created is False
    assert host2 is host
    assert "RCON-0001" not in reg._grace_tasks or reg._grace_tasks["RCON-0001"].done()

    host_module.DISCONNECT_GRACE_SECONDS = original


async def test_second_disconnect_creates_new_timer():
    """After reconnect cancels the timer, a subsequent disconnect must re-schedule."""
    from vibony_backend.agent_host import host as host_module
    from vibony_backend.agent_host.host import AgentHostRegistry

    original = host_module.DISCONNECT_GRACE_SECONDS
    host_module.DISCONNECT_GRACE_SECONDS = 10

    reg = AgentHostRegistry()
    fake_host = MagicMock()
    fake_host.terminate = AsyncMock()
    reg._hosts["RCON-0002"] = fake_host
    reg._connect_epochs["RCON-0002"] = 1

    # First disconnect + reconnect cycle.
    await reg.connection_closed("RCON-0002", 1)
    reg.cancel_grace("RCON-0002")

    # Second disconnect — new timer must exist.
    await reg.connection_closed("RCON-0002", 1)
    assert "RCON-0002" in reg._grace_tasks
    task = reg._grace_tasks["RCON-0002"]
    assert not task.done()

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    host_module.DISCONNECT_GRACE_SECONDS = original


async def test_grace_fires_destroys_sandbox():
    """When the grace timer fires, the host is terminated and removed from registry."""
    from vibony_backend.agent_host import host as host_module
    from vibony_backend.agent_host.host import AgentHostRegistry

    original = host_module.DISCONNECT_GRACE_SECONDS
    host_module.DISCONNECT_GRACE_SECONDS = 0  # fire immediately

    reg = AgentHostRegistry()
    fake_host = MagicMock()
    fake_host.terminate = AsyncMock()
    reg._hosts["GRAC-0001"] = fake_host
    reg._connect_epochs["GRAC-0001"] = 1

    await reg.connection_closed("GRAC-0001", 1)
    await asyncio.sleep(0.05)  # let event loop process the grace task

    fake_host.terminate.assert_awaited_once()
    assert reg.get("GRAC-0001") is None

    host_module.DISCONNECT_GRACE_SECONDS = original


# ---------------------------------------------------------------------------
# Message persistence across cold-starts
# ---------------------------------------------------------------------------


async def test_restore_messages_populated_on_cold_start():
    """_restore_messages() populates _messages when event store has history."""
    prior_history = [
        {"role": "user", "content": "help me write train.py"},
        {"role": "assistant", "content": "Sure, I will write train.py for you."},
        {"role": "user", "content": "add early stopping"},
        {"role": "assistant", "content": "Done, early stopping is added."},
    ]
    host = _make_host(session_code="COLD-0001")

    with (
        patch.dict("os.environ", {"VIBONY_SANDBOX_PROVIDER": "local"}),
        patch("vibony_backend.agent_host.host.store_event", new_callable=AsyncMock, return_value=1),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
        patch(
            "vibony_backend.agent_host.host.load_agent_messages",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "vibony_backend.agent_host.host.replay_messages_for_agent",
            new_callable=AsyncMock,
            return_value=prior_history,
        ),
    ):
        await host.start()

    assert host._messages == prior_history


async def test_restore_messages_noop_when_no_history():
    """_restore_messages() leaves _messages empty for a brand-new session."""
    host = _make_host(session_code="COLD-0002")

    with (
        patch.dict("os.environ", {"VIBONY_SANDBOX_PROVIDER": "local"}),
        patch("vibony_backend.agent_host.host.store_event", new_callable=AsyncMock, return_value=1),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
        patch(
            "vibony_backend.agent_host.host.load_agent_messages",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "vibony_backend.agent_host.host.replay_messages_for_agent",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        await host.start()

    assert host._messages == []


async def test_restore_messages_failure_stops_start():
    """A storage outage must not silently restart the conversation from scratch."""
    from vibony_backend.sessions.runtime_store import PersistenceError
    host = _make_host(session_code="COLD-0003")

    with (
        patch.dict("os.environ", {"VIBONY_SANDBOX_PROVIDER": "local"}),
        patch("vibony_backend.agent_host.host.store_event", new_callable=AsyncMock, return_value=1),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
        patch(
            "vibony_backend.agent_host.host.load_agent_messages",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "vibony_backend.agent_host.host.replay_messages_for_agent",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB unavailable"),
        ),
    ):
        with pytest.raises(PersistenceError):
            await host.start()

    assert host._started is True
    assert host._messages == []


async def test_cold_restart_passes_history_to_chat_turn():
    """Behavioral: after cold-start with history, chat_turn receives prior messages.

    Simulates backend restart:
      1. Host A runs a two-message conversation (history stored in DB).
      2. Host B is created with the same session_code.
      3. replay_messages_for_agent returns that history.
      4. Host B's first chat_turn invocation sees the full prior context.
    """
    # Keep a static copy as the ground-truth expectation so mutations in
    # fake_chat_turn don't affect the comparison (host._messages is the same
    # list object that replay_messages_for_agent returns, and fake_chat_turn
    # appends to it during the turn).
    prior_history_snapshot = [
        {"role": "user", "content": "implement checkpoint saving"},
        {"role": "assistant", "content": "I have implemented checkpoint saving."},
        {"role": "user", "content": "now add resume logic"},
        {"role": "assistant", "content": "Resume logic added."},
    ]

    host_b = _make_host(session_code="COLD-0004")

    # Capture messages passed to chat_turn on the first invocation.
    captured: list[list] = []

    async def fake_chat_turn(toolset, task_id, messages, user_text, **kwargs):
        # Snapshot messages BEFORE any mutation so we see what the host passed in.
        captured.append(list(messages))
        # Simulate what a real chat_turn does (appends user + assistant).
        messages.append({"role": "user", "content": user_text})
        messages.append({"role": "assistant", "content": "will do"})

    with (
        patch.dict("os.environ", {"VIBONY_SANDBOX_PROVIDER": "local"}),
        patch("vibony_backend.agent_host.host.store_event", new_callable=AsyncMock, return_value=1),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
        patch(
            "vibony_backend.agent_host.host.load_agent_messages",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "vibony_backend.agent_host.host.replay_messages_for_agent",
            new_callable=AsyncMock,
            return_value=list(prior_history_snapshot),  # fresh list so mutations don't affect the snapshot
        ),
        patch("vibony_backend.agent_host.host.chat_turn", side_effect=fake_chat_turn),
    ):
        await host_b.start()
        # _messages must equal the restored history before the first turn.
        assert host_b._messages[:4] == prior_history_snapshot

        await host_b.submit_user_message("also save best weights separately")

    assert len(captured) == 1, "chat_turn should have been called exactly once"
    assert captured[0] == prior_history_snapshot, (
        "chat_turn must receive prior_history as the initial messages list"
    )


# ---------------------------------------------------------------------------
# Sandbox files survive reconnect (1 h grace, expired flag)
# ---------------------------------------------------------------------------


async def test_grace_expired_marks_code_as_expired():
    """When grace fires, the code is added to _expired_codes."""
    from vibony_backend.agent_host import host as host_module
    from vibony_backend.agent_host.host import AgentHostRegistry

    original = host_module.DISCONNECT_GRACE_SECONDS
    try:
        host_module.DISCONNECT_GRACE_SECONDS = 0  # fire immediately

        reg = AgentHostRegistry()
        fake_host = MagicMock()
        fake_host.terminate = AsyncMock()
        reg._hosts["EXP-0001"] = fake_host
        reg._connect_epochs["EXP-0001"] = 1

        await reg.connection_closed("EXP-0001", 1)
        await asyncio.sleep(0.05)

        assert "EXP-0001" in reg._expired_codes, "expired code must be recorded after grace fires"
    finally:
        host_module.DISCONNECT_GRACE_SECONDS = original


async def test_reconnect_after_grace_expired_flags_new_host():
    """After grace fires, a new host created via get_or_create has _sandbox_was_expired=True."""
    from vibony_backend.agent_host import host as host_module
    from vibony_backend.agent_host.host import AgentHostRegistry

    original = host_module.DISCONNECT_GRACE_SECONDS
    try:
        host_module.DISCONNECT_GRACE_SECONDS = 0  # fire immediately

        reg = AgentHostRegistry()
        fake_host = MagicMock()
        fake_host.terminate = AsyncMock()
        reg._hosts["EXP-0002"] = fake_host
        reg._connect_epochs["EXP-0002"] = 1

        await reg.connection_closed("EXP-0002", 1)
        await asyncio.sleep(0.05)  # grace fires

        # Reconnect creates a new host.
        session_id = uuid.uuid4()
        ai_config = {"primary_model": "test-model"}
        new_host, was_created, _ = await reg.get_or_create(
            "EXP-0002", session_id, "task-01-training-loop", ai_config
        )

        assert was_created is True
        assert new_host._sandbox_was_expired is True, "new host must know the sandbox expired"
        assert "EXP-0002" not in reg._expired_codes, "expired flag consumed on get_or_create"
    finally:
        host_module.DISCONNECT_GRACE_SECONDS = original


async def test_reconnect_within_grace_not_marked_expired():
    """Normal reconnect within the grace window must NOT set _sandbox_was_expired."""
    from vibony_backend.agent_host import host as host_module
    from vibony_backend.agent_host.host import AgentHostRegistry

    original = host_module.DISCONNECT_GRACE_SECONDS
    try:
        host_module.DISCONNECT_GRACE_SECONDS = 10  # long enough to not fire

        reg = AgentHostRegistry()
        session_id = uuid.uuid4()
        ai_config = {"primary_model": "test-model"}

        host, _, epoch = await reg.get_or_create("EXP-0003", session_id, "task-01-training-loop", ai_config)
        await reg.connection_closed("EXP-0003", epoch)

        # Reconnect within grace window.
        same_host, was_created, _ = await reg.get_or_create(
            "EXP-0003", session_id, "task-01-training-loop", ai_config
        )

        assert was_created is False
        assert same_host is host
        assert same_host._sandbox_was_expired is False
    finally:
        reg._cancel_grace("EXP-0003")
        host_module.DISCONNECT_GRACE_SECONDS = original


async def test_expired_sandbox_emits_sandbox_reset_event():
    """_emit_start_events emits sandbox_reset{reason:'expired'} when _sandbox_was_expired."""
    import io
    import tarfile

    def _empty_tgz() -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz"):
            pass
        return buf.getvalue()

    host = _make_host(session_code="EXP-0004")
    host._sandbox_was_expired = True

    emitted: list[tuple[str, dict]] = []

    async def fake_store_event(session_id, session_code, event_type, payload):
        emitted.append((event_type, payload))
        return len(emitted)

    with (
        patch.dict("os.environ", {"VIBONY_SANDBOX_PROVIDER": "local"}),
        patch("vibony_backend.agent_host.host.store_event", side_effect=fake_store_event),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
        patch(
            "vibony_backend.agent_host.host.load_agent_messages",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "vibony_backend.agent_host.host.replay_messages_for_agent",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "vibony_backend.agent_host.host.has_assistant_message",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        await host.start()

    event_types = [e[0] for e in emitted]
    assert "sandbox_reset" in event_types, "sandbox_reset must be emitted on expired start"
    sr_payload = next(p for t, p in emitted if t == "sandbox_reset")
    assert sr_payload.get("reason") == "expired"

pytestmark = [pytest.mark.asyncio(loop_scope="session"), pytest.mark.usefixtures("isolated_agent_storage")]
