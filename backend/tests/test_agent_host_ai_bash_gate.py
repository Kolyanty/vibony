"""Tests for AI-bash permission gate in SandboxToolset and AgentHost.

Covers:
  - SandboxToolset.bash() respects bash_gate_fn callback
  - AgentHost._gate_ai_bash(): safe / deny / confirm round-trips / no-WS auto-deny
  - allow_prefix persistence shared with submit_inline_bash state
  - Gate wired on _provision_docker
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _default_exec_stream(handle, command, on_chunk, *, cwd=None, timeout_ms=120_000, tty=True):
    from vibony_backend.sandbox.docker_provider import ExecResult
    await on_chunk(b"ok\n")
    return ExecResult(stdout="ok\n", stderr="", exit_code=0, duration_ms=5, timed_out=False)


def _make_sandbox_toolset(gate_fn=None):
    """Return (toolset, provider_mock, sink_calls)."""
    from vibony_backend.sandbox.docker_provider import SandboxHandle
    from vibony_backend.sandbox.toolset import SandboxToolset

    handle = SandboxHandle(sandbox_id="fake-container")
    provider = AsyncMock()
    # bash() now uses exec_stream; exec is kept for glob/grep but not tested here.
    provider.exec_stream = AsyncMock(side_effect=_default_exec_stream)

    sink_calls: list[tuple[str, dict]] = []

    async def _emit(t, p):
        sink_calls.append((t, p))

    sink = MagicMock()
    sink.emit = AsyncMock(side_effect=_emit)

    toolset = SandboxToolset(provider, handle, sink, bash_gate_fn=gate_fn)
    return toolset, provider, sink_calls


def _make_host(**kwargs):
    from vibony_backend.agent_host.host import AgentHost

    return AgentHost(
        session_id=kwargs.get("session_id", uuid.uuid4()),
        session_code=kwargs.get("session_code", "TEST-8141"),
        task_id=kwargs.get("task_id", "task-01-training-loop"),
        ai_config=kwargs.get(
            "ai_config",
            {"primary_model": "test-model"},
        ),
    )


async def _resolve_after(host, cmd_id: str, choice: str, delay: float = 0.0) -> None:
    if delay:
        await asyncio.sleep(delay)
    host.resolve_bash_confirm(cmd_id, choice)


# ---------------------------------------------------------------------------
# SandboxToolset: gate callback integration
# ---------------------------------------------------------------------------


async def test_toolset_gate_allow_executes():
    """When gate_fn returns auto_allow, exec_stream is called and tool_result is success."""
    async def _allow(cmd, cmd_id):
        return "auto_allow", False

    toolset, provider, sink_calls = _make_sandbox_toolset(gate_fn=_allow)
    result = await toolset.bash("pip install numpy")

    provider.exec_stream.assert_awaited_once()
    assert result.exit_code == 0
    tool_call = next(p for t, p in sink_calls if t == "tool_call")
    assert tool_call["permission_decision"] == "auto_allow"
    tool_result = next(p for t, p in sink_calls if t == "tool_result")
    assert tool_result["is_error"] is False


async def test_toolset_gate_deny_raises_and_no_exec():
    """When gate_fn returns policy_deny, exec_stream is NOT called and ToolsetError is raised."""
    from vibony_backend.sandbox.toolset import ToolsetError

    async def _deny(cmd, cmd_id):
        return "policy_deny", False

    toolset, provider, sink_calls = _make_sandbox_toolset(gate_fn=_deny)
    with pytest.raises(ToolsetError, match="blocked"):
        await toolset.bash("rm -rf /")

    provider.exec_stream.assert_not_awaited()
    tool_call = next(p for t, p in sink_calls if t == "tool_call")
    assert tool_call["permission_decision"] == "policy_deny"
    assert "prompted" not in tool_call
    tool_result = next(p for t, p in sink_calls if t == "tool_result")
    assert tool_result["is_error"] is True


async def test_toolset_gate_fn_receives_command_and_call_id():
    """gate_fn is called with the command string and a non-empty call_id."""
    received: list[tuple[str, str]] = []

    async def _capture(cmd, cmd_id):
        received.append((cmd, cmd_id))
        return "auto_allow", False

    toolset, _provider, _calls = _make_sandbox_toolset(gate_fn=_capture)
    await toolset.bash("echo hi")

    assert len(received) == 1
    assert received[0][0] == "echo hi"
    assert received[0][1]  # non-empty call_id


async def test_toolset_gate_user_allow_sets_prompted():
    """When gate returns user_allow, tool_call includes prompted=True."""
    async def _user_allow(cmd, cmd_id):
        return "user_allow", True

    toolset, _provider, sink_calls = _make_sandbox_toolset(gate_fn=_user_allow)
    await toolset.bash("pip install numpy")

    tool_call = next(p for t, p in sink_calls if t == "tool_call")
    assert tool_call["permission_decision"] == "user_allow"
    assert tool_call.get("prompted") is True


async def test_toolset_no_gate_fn_executes_freely():
    """When bash_gate_fn is None, bash auto-allows and marks permission_decision accordingly."""
    toolset, provider, sink_calls = _make_sandbox_toolset(gate_fn=None)
    await toolset.bash("rm -rf /")  # no gate — executes
    provider.exec_stream.assert_awaited_once()
    tool_call = next(p for t, p in sink_calls if t == "tool_call")
    assert tool_call["permission_decision"] == "auto_allow"
    tool_result = next(p for t, p in sink_calls if t == "tool_result")
    assert tool_result["is_error"] is False


# ---------------------------------------------------------------------------
# AgentHost._gate_ai_bash: logic tests
# ---------------------------------------------------------------------------


async def test_gate_ai_bash_safe_cmd_allow():
    """Whitelisted commands (ls, pytest, echo) are auto-allowed without WS roundtrip."""
    host = _make_host()
    for cmd in ("ls -la", "pytest", "echo hello", "git status"):
        decision, prompted = await host._gate_ai_bash(cmd, uuid.uuid4().hex)
        assert decision == "auto_allow", f"expected auto_allow for {cmd!r}, got {decision!r}"
        assert prompted is False


async def test_gate_ai_bash_policy_deny():
    """Dangerous commands (rm -rf /) are denied immediately without WS roundtrip."""
    host = _make_host()
    decision, prompted = await host._gate_ai_bash("rm -rf /", uuid.uuid4().hex)
    assert decision == "policy_deny"
    assert prompted is False


async def test_gate_ai_bash_no_ws_auto_deny():
    """confirm-class command with no WS → immediate policy_deny (not 300s timeout)."""
    host = _make_host()
    # _ws_send is None by default; mkdir is a confirm-class command
    decision, prompted = await host._gate_ai_bash("mkdir /newdir", uuid.uuid4().hex)
    assert decision == "policy_deny"
    assert prompted is False


async def test_gate_ai_bash_confirm_allow_once():
    """confirm path: bash_confirm_request sent, user responds allow_once → user_allow."""
    host = _make_host()
    sent_frames: list[dict] = []

    async def fake_ws(frame):
        sent_frames.append(frame)
        if frame.get("kind") == "bash_confirm_request":
            asyncio.create_task(_resolve_after(host, frame["cmd_id"], "allow_once"))

    host.set_ws_send(fake_ws)
    decision, prompted = await host._gate_ai_bash("pip install numpy", uuid.uuid4().hex)

    assert decision == "user_allow"
    assert prompted is True
    confirm_frames = [f for f in sent_frames if f.get("kind") == "bash_confirm_request"]
    assert len(confirm_frames) == 1
    assert confirm_frames[0]["cmd"] == "pip install numpy"


async def test_gate_ai_bash_confirm_deny():
    """confirm path: user responds deny → user_deny with prompted=True."""
    host = _make_host()

    async def fake_ws(frame):
        if frame.get("kind") == "bash_confirm_request":
            asyncio.create_task(_resolve_after(host, frame["cmd_id"], "deny"))

    host.set_ws_send(fake_ws)
    decision, prompted = await host._gate_ai_bash("pip install numpy", uuid.uuid4().hex)
    assert decision == "user_deny"
    assert prompted is True


async def test_gate_ai_bash_allow_prefix_persists():
    """allow_prefix saves prefix; subsequent matching command is auto_allow."""
    host = _make_host()
    sent_frames: list[dict] = []
    call_count = {"n": 0}

    async def fake_ws(frame):
        sent_frames.append(frame)
        if frame.get("kind") == "bash_confirm_request":
            call_count["n"] += 1
            asyncio.create_task(_resolve_after(host, frame["cmd_id"], "allow_prefix"))

    host.set_ws_send(fake_ws)

    # First command triggers confirm; user chooses allow_prefix.
    d1, _ = await host._gate_ai_bash("pip install numpy", uuid.uuid4().hex)
    assert d1 == "user_allow"
    assert call_count["n"] == 1

    # Second pip install should be auto_allow (prefix saved).
    d2, prompted2 = await host._gate_ai_bash("pip install pandas", uuid.uuid4().hex)
    assert d2 == "auto_allow"
    assert prompted2 is False
    assert call_count["n"] == 1  # no new confirm sent


# ---------------------------------------------------------------------------
# Integration: gate is wired in AgentHost._provision_docker
# ---------------------------------------------------------------------------


async def test_provision_docker_wires_gate():
    """_provision_docker injects _gate_ai_bash into SandboxToolset."""
    host = _make_host()

    with (
        patch("vibony_backend.agent_host.host.store_event", new_callable=AsyncMock),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
        patch(
            "vibony_backend.sandbox.docker_provider.DockerSandboxProvider"
        ) as MockProvider,
    ):
        from vibony_backend.sandbox.docker_provider import SandboxHandle

        mock_provider = AsyncMock()
        mock_provider.create = AsyncMock(return_value=SandboxHandle(sandbox_id="abc"))
        MockProvider.return_value = mock_provider

        await host._provision_docker()

    from vibony_backend.sandbox.toolset import SandboxToolset

    assert isinstance(host._toolset, SandboxToolset)
    assert host._toolset._bash_gate_fn is not None
    # Bound methods create a new object on each access; compare __func__ + __self__.
    assert host._toolset._bash_gate_fn.__func__ is host._gate_ai_bash.__func__
    assert host._toolset._bash_gate_fn.__self__ is host
