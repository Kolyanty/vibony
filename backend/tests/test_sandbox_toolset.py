"""Unit tests for SandboxToolset — pure Python, no Docker daemon needed.

All provider interactions are replaced by AsyncMock so these tests run in any
environment. They verify routing (correct provider method called), event
emission, and exception translation — not sandbox behaviour.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _default_exec_stream(handle, command, on_chunk, *, cwd=None, timeout_ms=120_000, tty=True):
    """Default exec_stream mock: delivers one chunk then returns ExecResult."""
    from vibony_backend.sandbox.docker_provider import ExecResult

    await on_chunk(b"hello\n")
    return ExecResult(stdout="hello\n", stderr="", exit_code=0, duration_ms=10, timed_out=False)


def _make_toolset(provider_overrides: dict | None = None):
    """Return (toolset, provider_mock, sink_calls)."""
    from vibony_backend.sandbox.docker_provider import ExecResult, SandboxHandle
    from vibony_backend.sandbox.toolset import SandboxToolset

    handle = SandboxHandle(sandbox_id="fake-container-id")

    provider = AsyncMock()
    # Set sensible defaults; individual tests can override.
    provider.read_file = AsyncMock(return_value="file content\n")
    provider.write_file = AsyncMock(return_value=None)
    provider.exec = AsyncMock(
        return_value=ExecResult(
            stdout="hello\n",
            stderr="",
            exit_code=0,
            duration_ms=10,
            timed_out=False,
        )
    )
    # exec_stream is used by bash(); the default mock calls on_chunk once then returns.
    provider.exec_stream = AsyncMock(side_effect=_default_exec_stream)
    if provider_overrides:
        for attr, val in provider_overrides.items():
            setattr(provider, attr, val)

    sink_calls: list[tuple[str, dict]] = []

    async def _emit(event_type: str, payload: dict) -> None:
        sink_calls.append((event_type, payload))

    sink = MagicMock()
    sink.emit = AsyncMock(side_effect=_emit)

    toolset = SandboxToolset(provider, handle, sink)
    return toolset, provider, sink_calls


# ---------------------------------------------------------------------------
# read routes to provider.read_file
# ---------------------------------------------------------------------------


async def test_read_routes_to_provider():
    toolset, provider, sink_calls = _make_toolset()

    result = await toolset.read("train.py")

    provider.read_file.assert_awaited_once()
    call_args = provider.read_file.call_args
    assert call_args.args[1] == "train.py"  # path

    assert result == "file content\n"
    # Must emit tool_call then tool_result
    assert sink_calls[0][0] == "tool_call"
    assert sink_calls[0][1]["tool_name"] == "Read"
    assert sink_calls[1][0] == "tool_result"
    assert sink_calls[1][1]["is_error"] is False


async def test_read_translates_file_not_found():
    from vibony_backend.sandbox.toolset import ToolsetError

    toolset, provider, sink_calls = _make_toolset(
        {"read_file": AsyncMock(side_effect=FileNotFoundError("not found"))}
    )

    with pytest.raises(ToolsetError, match="FileNotFound"):
        await toolset.read("missing.py")

    assert sink_calls[-1][0] == "tool_result"
    assert sink_calls[-1][1]["is_error"] is True


# ---------------------------------------------------------------------------
# write routes to provider.write_file
# ---------------------------------------------------------------------------


async def test_write_routes_to_provider():
    toolset, provider, sink_calls = _make_toolset()

    await toolset.write("out.py", "print('hi')")

    provider.write_file.assert_awaited_once()
    call_args = provider.write_file.call_args
    assert call_args.args[1] == "out.py"
    assert call_args.args[2] == "print('hi')"

    tool_result = next(p for t, p in sink_calls if t == "tool_result")
    assert tool_result["is_error"] is False
    assert tool_result["output"] == "OK"
    # write now also emits file_changed
    assert any(t == "file_changed" for t, _ in sink_calls)


# ---------------------------------------------------------------------------
# bash routes to provider.exec_stream and emits console events
# ---------------------------------------------------------------------------


async def test_bash_routes_to_exec_stream():
    toolset, provider, sink_calls = _make_toolset()

    result = await toolset.bash("echo hello")

    provider.exec_stream.assert_awaited_once()
    call_args = provider.exec_stream.call_args
    assert call_args.args[1] == "echo hello"

    assert result.stdout == "hello\n"
    assert result.exit_code == 0

    types = [t for t, _ in sink_calls]
    # tool_call comes first (gate happens before emit_call)
    assert types[0] == "tool_call"
    assert sink_calls[0][1]["tool_name"] == "Bash"
    # command_run is emitted before exec so the console shows the command
    assert "command_run" in types
    # command_output arrives as chunks stream in
    assert "command_output" in types
    # command_finished closes the console entry
    assert "command_finished" in types
    # tool_result is last so the dialog shows the full output
    assert types[-1] == "tool_result"
    assert sink_calls[-1][1]["is_error"] is False


async def test_bash_console_events_order():
    """command_run → command_output → command_finished before tool_result."""
    toolset, provider, sink_calls = _make_toolset()

    await toolset.bash("echo hello")

    types = [t for t, _ in sink_calls]
    idx_run = types.index("command_run")
    idx_out = types.index("command_output")
    idx_fin = types.index("command_finished")
    idx_res = types.index("tool_result")
    assert idx_run < idx_out < idx_fin < idx_res


async def test_bash_exception_becomes_toolset_error():
    from vibony_backend.sandbox.toolset import ToolsetError

    async def _raise_exec_stream(handle, command, on_chunk, *, cwd=None, timeout_ms=120_000, tty=True):
        # A raw Docker-SDK style message that must NOT leak.
        raise RuntimeError("APIError: 500 /var/run/docker.sock container abc123def gone")

    toolset, provider, sink_calls = _make_toolset(
        {"exec_stream": AsyncMock(side_effect=_raise_exec_stream)}
    )

    # The raw exception string is sanitised to a stable, leak-free message.
    with pytest.raises(ToolsetError, match="sandbox operation failed"):
        await toolset.bash("ls")

    types = [t for t, _ in sink_calls]
    assert types[-1] == "tool_result"
    assert sink_calls[-1][1]["is_error"] is True
    # No container id / socket path leaks into the persisted tool_result.
    leak_bits = ("docker.sock", "abc123def", "APIError")
    for _, payload in sink_calls:
        for field in ("output",):
            if field in payload:
                assert not any(bit in payload[field] for bit in leak_bits)
    # command_finished is emitted with exit_code=-1 (infra failure, not a real
    # exit code) and timed_out derived from the exception, not hardcoded.
    assert "command_finished" in types
    fin_payload = next(p for t, p in sink_calls if t == "command_finished")
    assert fin_payload["exit_code"] == -1
    assert fin_payload["timed_out"] is False


async def test_bash_timeout_exception_marks_timed_out():
    """A TimeoutError from exec_stream sets timed_out=True."""
    from vibony_backend.sandbox.toolset import ToolsetError

    async def _timeout_exec_stream(handle, command, on_chunk, *, cwd=None, timeout_ms=120_000, tty=True):
        raise TimeoutError("exec timed out")

    toolset, provider, sink_calls = _make_toolset(
        {"exec_stream": AsyncMock(side_effect=_timeout_exec_stream)}
    )

    with pytest.raises(ToolsetError, match="command timed out"):
        await toolset.bash("sleep 999")

    fin_payload = next(p for t, p in sink_calls if t == "command_finished")
    assert fin_payload["exit_code"] == -1
    assert fin_payload["timed_out"] is True


# ---------------------------------------------------------------------------
# grep / glob confinement + ReDoS + timeout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("escape", ["../../etc", "../secrets", "/etc"])
async def test_grep_path_escape_rejected(escape):
    from vibony_backend.sandbox.toolset import ToolsetError

    toolset, provider, sink_calls = _make_toolset()

    with pytest.raises(ToolsetError, match="escapes working directory"):
        await toolset.grep("x", path=escape)

    # Confinement is enforced before any container exec runs.
    provider.exec.assert_not_awaited()
    assert sink_calls[-1][0] == "tool_result"
    assert sink_calls[-1][1]["is_error"] is True


async def test_grep_glob_pattern_escape_rejected():
    from vibony_backend.sandbox.toolset import ToolsetError

    toolset, provider, sink_calls = _make_toolset()

    with pytest.raises(ToolsetError, match="escapes working directory"):
        await toolset.grep("x", glob="../../*.py")

    provider.exec.assert_not_awaited()


@pytest.mark.parametrize("escape", ["../../etc/*", "/etc/*"])
async def test_glob_path_escape_rejected(escape):
    from vibony_backend.sandbox.toolset import ToolsetError

    toolset, provider, sink_calls = _make_toolset()

    with pytest.raises(ToolsetError, match="escapes working directory"):
        await toolset.glob(escape)

    provider.exec.assert_not_awaited()


async def test_grep_passes_confined_args_through():
    """A legitimate path/glob reaches exec normalised, not rejected."""
    toolset, provider, sink_calls = _make_toolset()

    await toolset.grep("needle", path="src/./sub", glob="**/*.py")

    provider.exec.assert_awaited_once()
    argv = provider.exec.call_args.args[1]
    # argv = ["python3", "-c", code, pattern, safe_path, safe_glob]
    assert argv[-3] == "needle"
    assert argv[-2] == "src/sub"  # normpath collapsed "./"
    assert argv[-1] == "**/*.py"
    # tighter timeout is applied to the search tool
    assert provider.exec.call_args.kwargs["timeout_ms"] == 15_000


async def test_grep_pattern_too_long_rejected():
    from vibony_backend.sandbox.toolset import ToolsetError

    toolset, provider, sink_calls = _make_toolset()

    with pytest.raises(ToolsetError, match="too long"):
        await toolset.grep("a" * 1001)

    provider.exec.assert_not_awaited()


async def test_grep_invalid_regex_rejected():
    from vibony_backend.sandbox.toolset import ToolsetError

    toolset, provider, sink_calls = _make_toolset()

    with pytest.raises(ToolsetError, match="invalid grep regex"):
        await toolset.grep("(unclosed")

    provider.exec.assert_not_awaited()


async def test_grep_timeout_surfaced():
    """A timed-out grep is reported, not silently '(no matches)'."""
    from vibony_backend.sandbox.docker_provider import ExecResult
    from vibony_backend.sandbox.toolset import ToolsetError

    toolset, provider, sink_calls = _make_toolset(
        {
            "exec": AsyncMock(
                return_value=ExecResult(
                    stdout="", stderr="", exit_code=-1, duration_ms=15000, timed_out=True
                )
            )
        }
    )

    with pytest.raises(ToolsetError, match="grep timed out"):
        await toolset.grep("evil")

    assert sink_calls[-1][1]["is_error"] is True


async def test_grep_exec_exception_sanitised():
    """A raw Docker-SDK exception in grep is collapsed to a stable message."""
    from vibony_backend.sandbox.toolset import ToolsetError

    toolset, provider, sink_calls = _make_toolset(
        {"exec": AsyncMock(side_effect=RuntimeError("NotFound: container abc123 /var/run/docker.sock"))}
    )

    with pytest.raises(ToolsetError, match="sandbox operation failed"):
        await toolset.grep("x")

    out = sink_calls[-1][1]["output"]
    assert "docker.sock" not in out and "abc123" not in out


async def test_read_exception_sanitised():
    """A raw provider error on read is collapsed; a path escape still surfaces."""
    from vibony_backend.sandbox.toolset import ToolsetError

    toolset, provider, sink_calls = _make_toolset(
        {"read_file": AsyncMock(side_effect=RuntimeError("APIError container deadbeef /var/run/docker.sock"))}
    )

    with pytest.raises(ToolsetError, match="sandbox operation failed"):
        await toolset.read("train.py")

    out = sink_calls[-1][1]["output"]
    assert "docker.sock" not in out and "deadbeef" not in out


async def test_read_path_escape_surfaces_message():
    """ValueError from _safe_path is a safe, surfaced message (user's own path)."""
    from vibony_backend.sandbox.toolset import ToolsetError

    toolset, provider, sink_calls = _make_toolset(
        {"read_file": AsyncMock(side_effect=ValueError("Path escapes working directory: '../x'"))}
    )

    with pytest.raises(ToolsetError, match="escapes working directory"):
        await toolset.read("../x")


# ---------------------------------------------------------------------------
# DockerSandboxProvider.read_file — timeout vs missing file
# ---------------------------------------------------------------------------


def _bare_provider():
    """A DockerSandboxProvider without __init__ (no docker daemon needed)."""
    from vibony_backend.sandbox.docker_provider import DockerSandboxProvider

    prov = DockerSandboxProvider.__new__(DockerSandboxProvider)
    prov._client = MagicMock()
    return prov


async def test_read_file_timeout_not_filenotfound():
    from vibony_backend.sandbox.docker_provider import ExecResult, SandboxHandle

    prov = _bare_provider()
    prov.exec = AsyncMock(
        return_value=ExecResult(
            stdout="", stderr="timed out", exit_code=-1, duration_ms=120000, timed_out=True
        )
    )

    # A timeout must not masquerade as a missing file (the file may well exist).
    with pytest.raises(TimeoutError):
        await prov.read_file(SandboxHandle("c"), "exists.py")


async def test_read_file_nonzero_is_filenotfound():
    from vibony_backend.sandbox.docker_provider import ExecResult, SandboxHandle

    prov = _bare_provider()
    prov.exec = AsyncMock(
        return_value=ExecResult(
            stdout="", stderr="No such file", exit_code=1, duration_ms=5, timed_out=False
        )
    )

    with pytest.raises(FileNotFoundError):
        await prov.read_file(SandboxHandle("c"), "missing.py")


async def test_read_file_success_returns_stdout():
    from vibony_backend.sandbox.docker_provider import ExecResult, SandboxHandle

    prov = _bare_provider()
    prov.exec = AsyncMock(
        return_value=ExecResult(
            stdout="hello\n", stderr="", exit_code=0, duration_ms=5, timed_out=False
        )
    )

    assert await prov.read_file(SandboxHandle("c"), "ok.py") == "hello\n"
