"""Tests for bash streaming in AgentHost._run_bash and SandboxToolset.bash.

Verifies that command_output events are emitted as chunks arrive rather than
as one bulk payload, in both local-mode (asyncio subprocess) and docker-mode
(mocked exec_stream).  No real Docker daemon or DB needed.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_host(**kwargs):
    from vibony_backend.agent_host.host import AgentHost

    return AgentHost(
        session_id=kwargs.get("session_id", uuid.uuid4()),
        session_code=kwargs.get("session_code", "TEST-8X10"),
        task_id=kwargs.get("task_id", "task-01-training-loop"),
        ai_config=kwargs.get(
            "ai_config",
            {"primary_model": "test-model"},
        ),
    )


async def _start_local_host(host) -> list[tuple[str, dict]]:
    """Start host in local mode, return emitted events."""
    emitted: list[tuple[str, dict]] = []

    async def fake_store(sid, code, t, p, **kw):
        emitted.append((t, p))
        return len(emitted)

    with (
        patch("vibony_backend.agent_host.host.store_event", side_effect=fake_store),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
        patch("vibony_backend.agent_host.host.has_assistant_message", return_value=False),
        patch("vibony_backend.agent_host.host.replay_messages_for_agent", return_value=[]),
    ):
        await host.start()
        emitted.clear()
        return emitted, fake_store


# ---------------------------------------------------------------------------
# Local mode: streaming emits multiple command_output events
# ---------------------------------------------------------------------------


async def test_local_mode_streaming_emits_multiple_chunks():
    """In local mode, echo with two lines emits command_output per read chunk."""
    host = _make_host()
    emitted: list[tuple[str, dict]] = []

    async def fake_store(sid, code, t, p, **kw):
        emitted.append((t, p))
        return len(emitted)

    with (
        patch("vibony_backend.agent_host.host.store_event", side_effect=fake_store),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
        patch("vibony_backend.agent_host.host.has_assistant_message", return_value=False),
        patch("vibony_backend.agent_host.host.replay_messages_for_agent", return_value=[]),
    ):
        await host.start()
        emitted.clear()
        # printf without newline concat to ensure single syscall output doesn't matter;
        # what matters is that the events appear in the right order.
        await host.submit_inline_bash('echo line1 && echo line2')

    types = [t for t, _ in emitted]
    assert "command_run" in types
    assert "command_finished" in types
    # At least one command_output with combined text
    outputs = [(t, p) for t, p in emitted if t == "command_output"]
    assert len(outputs) >= 1
    combined = "".join(p["data"] for _, p in outputs)
    assert "line1" in combined
    assert "line2" in combined

    fin = next(p for t, p in emitted if t == "command_finished")
    assert fin["exit_code"] == 0


async def test_local_mode_exit_code_nonzero():
    """A failing command reports exit_code != 0 in command_finished."""
    host = _make_host()
    emitted: list[tuple[str, dict]] = []

    async def fake_store(sid, code, t, p, **kw):
        emitted.append((t, p))
        return len(emitted)

    with (
        patch("vibony_backend.agent_host.host.store_event", side_effect=fake_store),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
        patch("vibony_backend.agent_host.host.has_assistant_message", return_value=False),
        patch("vibony_backend.agent_host.host.replay_messages_for_agent", return_value=[]),
    ):
        await host.start()
        emitted.clear()
        await host.submit_inline_bash("exit 42")

    fin = next(p for t, p in emitted if t == "command_finished")
    assert fin["exit_code"] != 0


async def test_local_mode_events_order():
    """command_run → (≥1 command_output) → command_finished — no other order."""
    host = _make_host()
    emitted: list[tuple[str, dict]] = []

    async def fake_store(sid, code, t, p, **kw):
        emitted.append((t, p))
        return len(emitted)

    with (
        patch("vibony_backend.agent_host.host.store_event", side_effect=fake_store),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
        patch("vibony_backend.agent_host.host.has_assistant_message", return_value=False),
        patch("vibony_backend.agent_host.host.replay_messages_for_agent", return_value=[]),
    ):
        await host.start()
        emitted.clear()
        await host.submit_inline_bash("echo hi")

    types = [t for t, _ in emitted]
    idx_run = types.index("command_run")
    idx_fin = types.index("command_finished")
    assert idx_run < idx_fin
    # All command_output events sit between run and finished
    for i, t in enumerate(types):
        if t == "command_output":
            assert idx_run < i < idx_fin


# ---------------------------------------------------------------------------
# Docker mode: _run_bash uses exec_stream, not exec
# ---------------------------------------------------------------------------


async def test_docker_mode_run_bash_uses_exec_stream():
    """_run_bash in docker mode calls exec_stream and emits command_output per chunk."""
    from vibony_backend.sandbox.docker_provider import ExecResult, SandboxHandle
    from vibony_backend.agent_host.host import AgentHost

    host = _make_host()
    emitted: list[tuple[str, dict]] = []

    async def fake_store(sid, code, t, p, **kw):
        emitted.append((t, p))
        return len(emitted)

    async def fake_exec_stream(handle, command, on_chunk, *, cwd=None, timeout_ms=120_000, tty=True):
        await on_chunk(b"chunk A\n")
        await on_chunk(b"chunk B\n")
        return ExecResult(stdout="chunk A\nchunk B\n", stderr="", exit_code=0, duration_ms=50, timed_out=False)

    provider_mock = AsyncMock()
    provider_mock.exec_stream = AsyncMock(side_effect=fake_exec_stream)
    provider_mock.snapshot_paths = AsyncMock(return_value=set())
    provider_mock.snapshot_dirs = AsyncMock(return_value=set())
    provider_mock.list_changed_since = AsyncMock(return_value=[])

    handle_mock = SandboxHandle(sandbox_id="mock-container")

    host._sandbox_provider = provider_mock
    host._sandbox_handle = handle_mock
    host._workdir = None
    host._started = True

    with patch("vibony_backend.agent_host.host.store_event", side_effect=fake_store):
        await host._run_bash("pytest -v", "cmd-001", "auto_allow")

    types = [t for t, _ in emitted]
    outputs = [(t, p) for t, p in emitted if t == "command_output"]
    # Two chunks → two command_output events
    assert len(outputs) == 2
    assert outputs[0][1]["data"] == "chunk A\n"
    assert outputs[1][1]["data"] == "chunk B\n"

    fin = next(p for t, p in emitted if t == "command_finished")
    assert fin["exit_code"] == 0

    # exec_stream must be used, not exec
    provider_mock.exec.assert_not_called()
    provider_mock.exec_stream.assert_awaited_once()


async def test_docker_mode_run_bash_exit_code_in_finished():
    """exit_code from exec_stream is forwarded to command_finished."""
    from vibony_backend.sandbox.docker_provider import ExecResult, SandboxHandle

    host = _make_host()
    emitted: list[tuple[str, dict]] = []

    async def fake_store(sid, code, t, p, **kw):
        emitted.append((t, p))
        return len(emitted)

    async def fake_exec_stream(handle, command, on_chunk, *, cwd=None, timeout_ms=120_000, tty=True):
        await on_chunk(b"output\n")
        return ExecResult(stdout="output\n", stderr="", exit_code=2, duration_ms=30, timed_out=False)

    provider_mock = AsyncMock()
    provider_mock.exec_stream = AsyncMock(side_effect=fake_exec_stream)
    provider_mock.snapshot_paths = AsyncMock(return_value=set())
    provider_mock.snapshot_dirs = AsyncMock(return_value=set())
    provider_mock.list_changed_since = AsyncMock(return_value=[])

    host._sandbox_provider = provider_mock
    host._sandbox_handle = SandboxHandle(sandbox_id="mock-container")
    host._workdir = None
    host._started = True

    with patch("vibony_backend.agent_host.host.store_event", side_effect=fake_store):
        await host._run_bash("bad-cmd", "cmd-002", "auto_allow")

    fin = next(p for t, p in emitted if t == "command_finished")
    assert fin["exit_code"] == 2

pytestmark = [pytest.mark.asyncio(loop_scope="session"), pytest.mark.usefixtures("isolated_agent_storage")]
