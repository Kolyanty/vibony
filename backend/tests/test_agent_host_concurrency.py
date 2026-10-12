"""WS concurrency bounds on AgentHost.

start_user_message / start_inline_bash are single-flight — a second
concurrent request is rejected (returns False → the recv loop sends BUSY) instead
of piling up unbounded coroutines / concurrent execs. terminate() cancels an
in-flight turn / inline-bash task so it stops calling the LLM / touching the
sandbox once the session is terminal.

store_event + _load_loaded_task are patched so no DB / LLM is required (mirrors
test_agent_host_inline_bash).
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest



def _make_host():
    from vibony_backend.agent_host.host import AgentHost

    return AgentHost(
        session_id=uuid.uuid4(),
        session_code="TEST-1760",
        task_id="task-01-training-loop",
        ai_config={"primary_model": "test-model"},
    )


async def _fake_store(sid, code, t, p, **kw):
    return 1


# ---------------------------------------------------------------------------
# user_message single-flight
# ---------------------------------------------------------------------------


async def test_start_user_message_single_flight() -> None:
    host = _make_host()
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_chat_turn(**_kwargs):
        started.set()
        await release.wait()

    with (
        patch("vibony_backend.agent_host.host.store_event", side_effect=_fake_store),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
        patch("vibony_backend.agent_host.host.chat_turn", side_effect=fake_chat_turn),
    ):
        await host.start()

        assert host.start_user_message("first") is True
        await asyncio.wait_for(started.wait(), timeout=1.0)

        # Second turn while the first is in flight → rejected (single-flight).
        assert host.start_user_message("second") is False

        release.set()
        await asyncio.wait_for(host._turn_task, timeout=1.0)

        # After the first turn finishes, a new turn is accepted again.
        started.clear()
        release.clear()
        assert host.start_user_message("third") is True
        await asyncio.wait_for(started.wait(), timeout=1.0)
        release.set()
        await asyncio.wait_for(host._turn_task, timeout=1.0)


async def test_start_user_message_emits_user_message_event() -> None:
    """The user_message event is emitted from inside the turn (behind the lock)."""
    host = _make_host()
    emitted: list[tuple[str, dict]] = []

    async def capture_store(sid, code, t, p, **kw):
        emitted.append((t, p))
        return len(emitted)

    async def fake_chat_turn(**_kwargs):
        return None

    with (
        patch("vibony_backend.agent_host.host.store_event", side_effect=capture_store),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
        patch("vibony_backend.agent_host.host.chat_turn", side_effect=fake_chat_turn),
    ):
        await host.start()
        emitted.clear()
        assert host.start_user_message("hello AI") is True
        await asyncio.wait_for(host._turn_task, timeout=1.0)

    user_events = [p for t, p in emitted if t == "user_message"]
    assert user_events == [{"text": "hello AI"}]


# ---------------------------------------------------------------------------
# inline_bash single-flight
# ---------------------------------------------------------------------------


async def test_start_inline_bash_single_flight() -> None:
    host = _make_host()
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_run_bash(cmd, command_id, permission_decision):
        started.set()
        await release.wait()

    with (
        patch("vibony_backend.agent_host.host.store_event", side_effect=_fake_store),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
    ):
        await host.start()
        host._run_bash = fake_run_bash  # type: ignore[assignment]

        assert host.start_inline_bash("echo hi") is True
        await asyncio.wait_for(started.wait(), timeout=1.0)

        # Second command while the first runs → rejected (serialised).
        assert host.start_inline_bash("echo again") is False

        release.set()
        await asyncio.wait_for(host._bash_task, timeout=1.0)


# ---------------------------------------------------------------------------
# terminate cancels the in-flight turn
# ---------------------------------------------------------------------------


async def test_terminate_cancels_in_flight_turn() -> None:
    host = _make_host()
    started = asyncio.Event()
    never = asyncio.Event()

    async def fake_chat_turn(**_kwargs):
        started.set()
        await never.wait()  # would hang forever without cancellation

    with (
        patch("vibony_backend.agent_host.host.store_event", side_effect=_fake_store),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
        patch("vibony_backend.agent_host.host.chat_turn", side_effect=fake_chat_turn),
    ):
        await host.start()
        assert host.start_user_message("long running") is True
        await asyncio.wait_for(started.wait(), timeout=1.0)

        turn_task = host._turn_task
        await host.terminate()

        # The in-flight turn was cancelled rather than left calling the LLM.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(turn_task, timeout=1.0)
    assert host._terminated is True


async def test_start_rejected_after_terminate() -> None:
    host = _make_host()

    with (
        patch("vibony_backend.agent_host.host.store_event", side_effect=_fake_store),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
    ):
        await host.start()
        await host.terminate()

    assert host.start_user_message("nope") is False
    assert host.start_inline_bash("echo nope") is False

pytestmark = [pytest.mark.asyncio(loop_scope="session"), pytest.mark.usefixtures("isolated_agent_storage")]
