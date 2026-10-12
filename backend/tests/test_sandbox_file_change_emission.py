"""Tests for sandbox-side file_changed emission.

Verifies:
  - SandboxToolset.write / .edit emit file_changed events.
  - AgentHost._run_bash (docker mode) emits file_changed for files the bash
    command wrote, by calling provider.list_changed_since + read_file.
  - list_changed_since applies a 1-second clock-skew buffer.
  - rm file.py → file_changed {op: 'deleted'} event.
  - mkdir dir/ → file_changed {op: 'created', is_dir: true}.
  - rmdir dir/ → file_changed {op: 'deleted', is_dir: true}.
  - All emit for both !cmd (candidate) and AI-initiated bash paths.

All tests use AsyncMock / fake providers — no Docker daemon required.
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_toolset(*, write_ok=True, read_content="updated content\n"):
    """Return (toolset, provider_mock, sink_calls)."""
    from vibony_backend.sandbox.docker_provider import ExecResult, SandboxHandle
    from vibony_backend.sandbox.toolset import SandboxToolset

    handle = SandboxHandle(sandbox_id="fake-id")

    provider = AsyncMock()
    provider.read_file = AsyncMock(return_value=read_content)
    provider.write_file = AsyncMock(return_value=None)
    provider.exec = AsyncMock(
        return_value=ExecResult(
            stdout="", stderr="", exit_code=0, duration_ms=5, timed_out=False
        )
    )

    sink_calls: list[tuple[str, dict]] = []

    async def _emit(event_type: str, payload: dict) -> None:
        sink_calls.append((event_type, payload))

    sink = MagicMock()
    sink.emit = AsyncMock(side_effect=_emit)

    toolset = SandboxToolset(provider, handle, sink)
    return toolset, provider, sink_calls


def _make_docker_host():
    """Return (host, emitted) with docker sandbox provider mocked."""
    from vibony_backend.agent_host.host import AgentHost
    from vibony_backend.sandbox.docker_provider import ExecResult, SandboxHandle

    host = AgentHost(
        session_id=uuid.uuid4(),
        session_code="TEST-8600",
        task_id="task-01-training-loop",
        ai_config={"primary_model": "test-model"},
    )

    emitted: list[tuple[str, dict]] = []

    async def fake_store(sid, code, t, p, **kw):
        emitted.append((t, p))
        return len(emitted)

    return host, emitted, fake_store


# ---------------------------------------------------------------------------
# SandboxToolset.write emits file_changed
# ---------------------------------------------------------------------------


async def test_write_emits_file_changed():
    toolset, provider, sink_calls = _make_toolset()

    await toolset.write("train.py", "import torch\n")

    changed = [p for t, p in sink_calls if t == "file_changed"]
    assert len(changed) == 1
    ev = changed[0]
    assert ev["file_path"] == "train.py"
    assert ev["source"] == "agent"
    assert ev["content"] == "import torch\n"
    assert ev["diff"] is None


# ---------------------------------------------------------------------------
# SandboxToolset.edit emits file_changed
# ---------------------------------------------------------------------------


async def test_edit_emits_file_changed():
    toolset, provider, sink_calls = _make_toolset(read_content="x = 1\n")

    await toolset.edit("config.py", "x = 1", "x = 42")

    changed = [p for t, p in sink_calls if t == "file_changed"]
    assert len(changed) == 1
    ev = changed[0]
    assert ev["file_path"] == "config.py"
    assert ev["source"] == "agent"
    assert "x = 42" in ev["content"]
    assert ev["diff"] is None


# ---------------------------------------------------------------------------
# AgentHost._run_bash (docker mode) emits file_changed after exec
# ---------------------------------------------------------------------------


async def test_run_bash_docker_emits_file_changed():
    """After successful bash in docker mode, file_changed events emitted for changed paths."""
    from vibony_backend.sandbox.docker_provider import ExecResult, SandboxHandle

    host, emitted, fake_store = _make_docker_host()

    provider_mock = AsyncMock()
    handle = SandboxHandle(sandbox_id="fake-id")

    async def _fake_exec_stream(h, command, on_chunk, *, cwd=None, timeout_ms=120_000, tty=True):
        from vibony_backend.sandbox.docker_provider import ExecResult
        await on_chunk(b"done\n")
        return ExecResult(stdout="done\n", stderr="", exit_code=0, duration_ms=50, timed_out=False)

    provider_mock.exec_stream = AsyncMock(side_effect=_fake_exec_stream)
    provider_mock.list_changed_since = AsyncMock(return_value=["output.py"])
    provider_mock.read_file = AsyncMock(return_value="result = 42\n")
    # snapshot_paths/dirs: no deletions or dir changes
    provider_mock.snapshot_paths = AsyncMock(side_effect=[{"output.py"}, {"output.py"}])
    provider_mock.snapshot_dirs = AsyncMock(side_effect=[set(), set()])

    with patch("vibony_backend.agent_host.host.store_event", side_effect=fake_store):
        # Manually inject docker-mode state (bypass start() to avoid bundle resolution).
        host._started = True
        host._sandbox_provider = provider_mock
        host._sandbox_handle = handle
        # Need a toolset that won't crash _run_bash — but _run_bash uses provider directly.
        host._toolset = MagicMock()

        await host._run_bash("python gen.py", "cmd-001", "auto_allow")

    types = [t for t, _ in emitted]
    assert "command_run" in types
    assert "command_finished" in types
    assert "file_changed" in types

    changed_events = [p for t, p in emitted if t == "file_changed"]
    assert len(changed_events) == 1
    ev = changed_events[0]
    assert ev["file_path"] == "output.py"
    assert ev["source"] == "manual"
    assert ev["content"] == "result = 42\n"


# ---------------------------------------------------------------------------
# list_changed_since — multiple paths, clock-skew buffer
# ---------------------------------------------------------------------------


async def test_list_changed_since_applies_1s_buffer():
    """list_changed_since subtracts 1s from since_ts inside the container Python code."""
    from vibony_backend.sandbox.docker_provider import DockerSandboxProvider, ExecResult, SandboxHandle

    handle = SandboxHandle(sandbox_id="fake-id")

    captured_args: list = []

    async def fake_exec(h, command, **kwargs):
        captured_args.append(command)
        return ExecResult(
            stdout="model.py\nutils.py\n",
            stderr="",
            exit_code=0,
            duration_ms=5,
            timed_out=False,
        )

    provider = DockerSandboxProvider.__new__(DockerSandboxProvider)
    provider.exec = fake_exec  # type: ignore[attr-defined]

    since = time.time()
    paths = await provider.list_changed_since(handle, since)

    assert paths == ["model.py", "utils.py"]

    # The command passed to exec should contain the since_ts as argv[1].
    assert len(captured_args) == 1
    cmd = captured_args[0]
    # Should be a list starting with python3.
    assert cmd[0] == "python3"
    # since_ts is the last element.
    ts_arg = float(cmd[-1])
    assert abs(ts_arg - since) < 1.0

    # The Python code inside exec subtracts 1 from argv[1] (clock-skew buffer).
    code_str = cmd[2]
    assert "- 1" in code_str


# ---------------------------------------------------------------------------
# file_changed {op: 'deleted'} for rm via bash
# ---------------------------------------------------------------------------


def _make_docker_host_with_snapshots(pre_files, post_files, pre_dirs, post_dirs):
    """Return (host, emitted) with provider.snapshot_paths/dirs mocked."""
    from vibony_backend.agent_host.host import AgentHost
    from vibony_backend.sandbox.docker_provider import ExecResult, SandboxHandle

    host = AgentHost(
        session_id=uuid.uuid4(),
        session_code="TEST-1405",
        task_id="task-01-training-loop",
        ai_config={"primary_model": "test-model"},
    )

    emitted: list[tuple[str, dict]] = []

    async def fake_store(sid, code, t, p, **kw):
        emitted.append((t, p))
        return len(emitted)

    provider_mock = AsyncMock()
    handle = SandboxHandle(sandbox_id="fake-id")

    async def _fake_exec_stream(h, command, on_chunk, *, cwd=None, timeout_ms=120_000, tty=True):
        await on_chunk(b"done\n")
        return ExecResult(stdout="done\n", stderr="", exit_code=0, duration_ms=20, timed_out=False)

    provider_mock.exec_stream = AsyncMock(side_effect=_fake_exec_stream)
    provider_mock.list_changed_since = AsyncMock(return_value=[])
    provider_mock.snapshot_paths = AsyncMock(side_effect=[pre_files, post_files])
    provider_mock.snapshot_dirs = AsyncMock(side_effect=[pre_dirs, post_dirs])

    return host, emitted, fake_store, provider_mock, handle


async def test_run_bash_emits_file_changed_deleted_on_rm():
    """rm file.py via bash → file_changed {op: 'deleted'} event."""
    pre_files = {"train.py", "utils.py"}
    post_files = {"train.py"}  # utils.py deleted
    pre_dirs: set[str] = set()
    post_dirs: set[str] = set()

    host, emitted, fake_store, provider_mock, handle = _make_docker_host_with_snapshots(
        pre_files, post_files, pre_dirs, post_dirs
    )

    with patch("vibony_backend.agent_host.host.store_event", side_effect=fake_store):
        host._started = True
        host._sandbox_provider = provider_mock
        host._sandbox_handle = handle
        host._toolset = MagicMock()

        await host._run_bash("rm utils.py", "cmd-001", "auto_allow")

    deleted_events = [p for t, p in emitted if t == "file_changed"]
    assert len(deleted_events) == 1
    ev = deleted_events[0]
    assert ev["file_path"] == "utils.py"
    assert ev["op"] == "deleted"
    assert ev["content"] is None
    assert ev.get("is_dir") is not True


async def test_run_bash_emits_file_changed_created_dir_on_mkdir():
    """mkdir newdir/ via bash → file_changed {op: 'created', is_dir: True} event."""
    pre_files: set[str] = {"train.py"}
    post_files: set[str] = {"train.py"}
    pre_dirs: set[str] = set()
    post_dirs: set[str] = {"newdir"}  # new directory created

    host, emitted, fake_store, provider_mock, handle = _make_docker_host_with_snapshots(
        pre_files, post_files, pre_dirs, post_dirs
    )

    with patch("vibony_backend.agent_host.host.store_event", side_effect=fake_store):
        host._started = True
        host._sandbox_provider = provider_mock
        host._sandbox_handle = handle
        host._toolset = MagicMock()

        await host._run_bash("mkdir newdir", "cmd-002", "auto_allow")

    dir_events = [p for t, p in emitted if t == "file_changed"]
    assert len(dir_events) == 1
    ev = dir_events[0]
    assert ev["file_path"] == "newdir"
    assert ev["op"] == "created"
    assert ev["is_dir"] is True
    assert ev["content"] is None


async def test_run_bash_emits_file_changed_deleted_dir_on_rmdir():
    """rmdir dir/ via bash → file_changed {op: 'deleted', is_dir: True} event."""
    pre_files: set[str] = {"train.py"}
    post_files: set[str] = {"train.py"}
    pre_dirs: set[str] = {"olddir"}  # dir existed before
    post_dirs: set[str] = set()       # now gone

    host, emitted, fake_store, provider_mock, handle = _make_docker_host_with_snapshots(
        pre_files, post_files, pre_dirs, post_dirs
    )

    with patch("vibony_backend.agent_host.host.store_event", side_effect=fake_store):
        host._started = True
        host._sandbox_provider = provider_mock
        host._sandbox_handle = handle
        host._toolset = MagicMock()

        await host._run_bash("rmdir olddir", "cmd-003", "auto_allow")

    dir_events = [p for t, p in emitted if t == "file_changed"]
    assert len(dir_events) == 1
    ev = dir_events[0]
    assert ev["file_path"] == "olddir"
    assert ev["op"] == "deleted"
    assert ev["is_dir"] is True
    assert ev["content"] is None


async def test_run_bash_emits_multiple_events_mixed_changes():
    """A single bash command that creates a dir, modifies a file, and deletes another."""
    pre_files: set[str] = {"train.py", "old.py"}
    post_files: set[str] = {"train.py"}  # old.py deleted, train.py modified
    pre_dirs: set[str] = set()
    post_dirs: set[str] = {"models"}     # new dir

    host, emitted, fake_store, provider_mock, handle = _make_docker_host_with_snapshots(
        pre_files, post_files, pre_dirs, post_dirs
    )
    # list_changed_since also returns train.py as modified
    provider_mock.list_changed_since = AsyncMock(return_value=["train.py"])
    provider_mock.read_file = AsyncMock(return_value="# modified\n")

    with patch("vibony_backend.agent_host.host.store_event", side_effect=fake_store):
        host._started = True
        host._sandbox_provider = provider_mock
        host._sandbox_handle = handle
        host._toolset = MagicMock()

        await host._run_bash("some_script.sh", "cmd-004", "auto_allow")

    fc_events = [p for t, p in emitted if t == "file_changed"]
    ops = {(ev["file_path"], ev["op"], ev.get("is_dir", False)) for ev in fc_events}

    assert ("train.py", "modified", False) in ops
    assert ("old.py", "deleted", False) in ops
    assert ("models", "created", True) in ops


async def test_write_emits_op_modified():
    """SandboxToolset.write includes op='modified' in file_changed payload."""
    toolset, _provider, sink_calls = _make_toolset()
    await toolset.write("result.py", "x = 1\n")
    changed = [p for t, p in sink_calls if t == "file_changed"]
    assert len(changed) == 1
    assert changed[0].get("op") == "modified"


async def test_edit_emits_op_modified():
    """SandboxToolset.edit includes op='modified' in file_changed payload."""
    toolset, _provider, sink_calls = _make_toolset(read_content="x = 1\n")
    await toolset.edit("config.py", "x = 1", "x = 99")
    changed = [p for t, p in sink_calls if t == "file_changed"]
    assert len(changed) == 1
    assert changed[0].get("op") == "modified"


# ---------------------------------------------------------------------------
# snapshot_paths / snapshot_dirs — provider-level methods
# ---------------------------------------------------------------------------


async def test_snapshot_paths_returns_file_set():
    """snapshot_paths returns the set of non-private files in /work."""
    from vibony_backend.sandbox.docker_provider import DockerSandboxProvider, ExecResult, SandboxHandle

    handle = SandboxHandle(sandbox_id="fake-id")

    async def fake_exec(h, command, **kwargs):
        return ExecResult(
            stdout="train.py\nutils/helper.py\n",
            stderr="",
            exit_code=0,
            duration_ms=5,
            timed_out=False,
        )

    provider = DockerSandboxProvider.__new__(DockerSandboxProvider)
    provider.exec = fake_exec  # type: ignore[attr-defined]

    paths = await provider.snapshot_paths(handle)
    assert paths == {"train.py", "utils/helper.py"}


async def test_snapshot_dirs_returns_dir_set():
    """snapshot_dirs returns the set of non-private directories in /work."""
    from vibony_backend.sandbox.docker_provider import DockerSandboxProvider, ExecResult, SandboxHandle

    handle = SandboxHandle(sandbox_id="fake-id")

    async def fake_exec(h, command, **kwargs):
        return ExecResult(
            stdout="utils\nutils/sub\n",
            stderr="",
            exit_code=0,
            duration_ms=5,
            timed_out=False,
        )

    provider = DockerSandboxProvider.__new__(DockerSandboxProvider)
    provider.exec = fake_exec  # type: ignore[attr-defined]

    dirs = await provider.snapshot_dirs(handle)
    assert dirs == {"utils", "utils/sub"}

pytestmark = [pytest.mark.asyncio(loop_scope="session"), pytest.mark.usefixtures("isolated_agent_storage")]
