"""SandboxToolset — Toolset implementation backed by DockerSandboxProvider.

Emits tool_call / tool_result events to the injected EventSink, same as
BackendLocalToolset. Exception hierarchy matches LocalToolset so the agent
loop sees identical error shapes regardless of runtime.

glob() and grep() execute Python code directly in the container (no shell
escaping issues) via provider.exec(..., command=[list]).
"""

from __future__ import annotations

from vibony_backend.sessions.runtime_store import PersistenceError

import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn

from .docker_provider import DockerSandboxProvider, ExecResult, SandboxHandle, _safe_path

logger = logging.getLogger(__name__)


# ---------- Exception hierarchy (mirrors BackendLocalToolset) ----------

class ToolsetError(Exception):
    pass


class NoMatchError(ToolsetError):
    pass


class MultipleMatchesError(ToolsetError):
    pass


class PathOutsideWorkdirError(ToolsetError):
    pass


# ---------- Confinement + error hygiene helpers ----------

_GENERIC_TOOL_ERROR = "sandbox operation failed"

# Exception types whose messages derive only from the caller's own input
# (a path, a regex, our own timeout text) and are therefore safe to surface to
# the candidate/employer. Everything else (Docker SDK errors carrying container
# IDs / socket paths, unexpected failures) collapses to a generic message and is
# logged server-side instead.
_SAFE_EXCEPTIONS = (
    PathOutsideWorkdirError,
    NoMatchError,
    MultipleMatchesError,
    FileNotFoundError,
    TimeoutError,
    ValueError,  # only raised by _safe_path here — message is the user's path
)


def _confine(path: str) -> str:
    """Confine a path/glob pattern to the workdir (mirrors read/write).

    Reuses the provider's ``_safe_path`` so grep/glob enforce the same boundary
    as read/write. Returns the normalised relative path; raises
    PathOutsideWorkdirError if it escapes ``/work`` via ``..`` or an absolute path.
    """
    try:
        return _safe_path(path)
    except ValueError as exc:
        raise PathOutsideWorkdirError(str(exc)) from exc


def _safe_tool_error(exc: Exception) -> str:
    """Map an exception to a stable, leak-free message for the user."""
    if isinstance(exc, _SAFE_EXCEPTIONS):
        return str(exc)
    logger.warning("sandbox tool error: %s", type(exc).__name__, exc_info=exc)
    return _GENERIC_TOOL_ERROR


# ---------- Result types ----------

from dataclasses import dataclass


@dataclass
class BashResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    timed_out: bool


@dataclass
class GrepMatch:
    path: str
    line_number: int
    line: str


# ---------- SandboxToolset ----------

_GREP_MATCH_CAP = 200
# Reject overlong regexes before they reach the container (cheap ReDoS surface
# reduction). The timeout below bounds catastrophic backtracking on
# a short-but-evil pattern; real in-container process kill is a separate Linux concern.
_GREP_PATTERN_MAX_CHARS = 1000
# Tighter timeout for the read-only search tools so an expensive regex/glob can't
# pin a container CPU for the full 120s bash default.
_TOOL_EXEC_TIMEOUT_MS = 15_000


class SandboxToolset:
    """Toolset that routes operations to a running Docker sandbox container."""

    def __init__(
        self,
        provider: DockerSandboxProvider,
        handle: SandboxHandle,
        event_sink: Any,
        bash_gate_fn: Callable[[str, str], Awaitable[tuple[str, bool]]] | None = None,
    ) -> None:
        self._provider = provider
        self._handle = handle
        self._sink = event_sink
        self._bash_gate_fn = bash_gate_fn

    # ------------------------------------------------------------------
    # Event helpers
    # ------------------------------------------------------------------

    async def _emit_call(
        self,
        tool_name: str,
        input_: dict[str, Any],
        extra: dict[str, Any] | None = None,
        call_id: str | None = None,
    ) -> str:
        tool_call_id = call_id if call_id is not None else uuid.uuid4().hex
        payload: dict[str, Any] = {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "input": input_,
        }
        if extra:
            payload.update(extra)
        await self._sink.emit("tool_call", payload)
        return tool_call_id

    async def _emit_result(
        self,
        tool_call_id: str,
        output: Any,
        is_error: bool,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False, indent=2, default=str)
        payload: dict[str, Any] = {
            "tool_call_id": tool_call_id,
            "is_error": is_error,
            "output": output,
        }
        if extra:
            payload.update(extra)
        await self._sink.emit("tool_result", payload)

    async def _tool_failure(
        self, call_id: str, message: str, *, cause: Exception | None = None
    ) -> NoReturn:
        """Emit a leak-free error tool_result and raise ToolsetError."""
        await self._emit_result(call_id, message, is_error=True)
        raise ToolsetError(message) from cause

    # ------------------------------------------------------------------
    # Toolset API
    # ------------------------------------------------------------------

    async def read(self, path: str, offset: int = 0, limit: int | None = None) -> str:
        call_id = await self._emit_call("Read", {"path": path, "offset": offset, "limit": limit})
        try:
            content = await self._provider.read_file(self._handle, path)
            lines = content.splitlines(keepends=True)
            sliced = lines[offset:] if limit is None else lines[offset: offset + limit]
            result = "".join(sliced)
        except FileNotFoundError as exc:
            err = f"FileNotFound: {path!r}"
            await self._emit_result(call_id, err, is_error=True)
            raise ToolsetError(err) from exc
        except Exception as exc:
            await self._tool_failure(call_id, _safe_tool_error(exc), cause=exc)
        await self._emit_result(call_id, result, is_error=False)
        return result

    async def write(self, path: str, content: str) -> None:
        call_id = await self._emit_call("Write", {"path": path})
        try:
            await self._provider.write_file(self._handle, path, content)
        except Exception as exc:
            await self._tool_failure(call_id, _safe_tool_error(exc), cause=exc)
        await self._emit_result(call_id, "OK", is_error=False)
        await self._sink.emit(
            "file_changed",
            {"file_path": path, "diff": None, "source": "agent", "content": content, "op": "modified"},
        )

    async def edit(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> None:
        call_id = await self._emit_call(
            "Edit",
            {"path": path, "old_string": old_string, "replace_all": replace_all},
        )
        try:
            try:
                text = await self._provider.read_file(self._handle, path)
            except FileNotFoundError as exc:
                raise ToolsetError(f"FileNotFound: {path!r}") from exc
            except Exception as exc:
                raise ToolsetError(_safe_tool_error(exc)) from exc

            count = text.count(old_string)
            if count == 0:
                raise NoMatchError(f"No match for old_string in {path!r}")
            if count > 1 and not replace_all:
                raise MultipleMatchesError(
                    f"{count} matches for old_string in {path!r}; use replace_all=true"
                )
            updated = (
                text.replace(old_string, new_string)
                if replace_all
                else text.replace(old_string, new_string, 1)
            )
            try:
                await self._provider.write_file(self._handle, path, updated)
            except Exception as exc:
                raise ToolsetError(_safe_tool_error(exc)) from exc
        except ToolsetError as exc:
            await self._emit_result(call_id, str(exc), is_error=True)
            raise
        await self._emit_result(call_id, "OK", is_error=False)
        await self._sink.emit(
            "file_changed",
            {"file_path": path, "diff": None, "source": "agent", "content": updated, "op": "modified"},
        )

    async def bash(
        self,
        command: str,
        cwd: str | None = None,
        timeout_ms: int = 120_000,
    ) -> BashResult:
        # Generate call_id before gate so it can be reused as cmd_id in
        # bash_confirm_request — gate runs before tool_call emit (protocol contract).
        call_id = uuid.uuid4().hex

        call_extra: dict[str, Any] = {}
        if self._bash_gate_fn is not None:
            decision, prompted = await self._bash_gate_fn(command, call_id)
            call_extra["permission_decision"] = decision
            if prompted:
                call_extra["prompted"] = True
        else:
            call_extra["permission_decision"] = "auto_allow"

        await self._emit_call(
            "Bash",
            {"command": command, "cwd": cwd},
            extra=call_extra,
            call_id=call_id,
        )

        if call_extra.get("permission_decision") in ("policy_deny", "user_deny"):
            msg = "BashPolicyDeniedError: command blocked; try a different approach"
            await self._emit_result(call_id, msg, is_error=True)
            raise ToolsetError(msg)

        # Emit command_run so AI bash output appears in the candidate's console.
        await self._sink.emit("command_run", {
            "command_id": call_id,
            "command": command,
            "cwd": cwd or ".",
            "permission_decision": call_extra.get("permission_decision", "auto_allow"),
        })

        t_bash = time.monotonic()

        async def _on_chunk(chunk: bytes) -> None:
            text = chunk.decode("utf-8", errors="replace")
            await self._sink.emit("command_output", {
                "command_id": call_id,
                "stream": "stdout",
                "data": text,
            })

        try:
            exec_result: ExecResult = await self._provider.exec_stream(
                self._handle,
                command,
                _on_chunk,
                cwd=cwd,
                timeout_ms=timeout_ms,
            )
        except PersistenceError:
            raise
        except Exception as exc:
            # Derive exit_code/timed_out from the failure instead of hardcoding a
            # misleading exit_code=1/timed_out=False; -1 signals an
            # infra failure (the command never produced a real exit code).
            is_timeout = isinstance(exc, TimeoutError)
            await self._sink.emit("command_finished", {
                "command_id": call_id,
                "exit_code": -1,
                "duration_ms": int((time.monotonic() - t_bash) * 1000),
                "timed_out": is_timeout,
            })
            msg = "command timed out" if is_timeout else _safe_tool_error(exc)
            await self._tool_failure(call_id, msg, cause=exc)

        await self._sink.emit("command_finished", {
            "command_id": call_id,
            "exit_code": exec_result.exit_code,
            "duration_ms": exec_result.duration_ms,
            "timed_out": exec_result.timed_out,
        })

        result = BashResult(
            stdout=exec_result.stdout,
            stderr=exec_result.stderr,
            exit_code=exec_result.exit_code,
            duration_ms=exec_result.duration_ms,
            timed_out=exec_result.timed_out,
        )
        lines = [f"exit_code: {result.exit_code}", f"duration_ms: {result.duration_ms}"]
        if result.timed_out:
            lines.append("timed_out: true")
        if result.stdout:
            lines.append(f"stdout:\n{result.stdout}")
        if result.stderr:
            lines.append(f"stderr:\n{result.stderr}")
        await self._emit_result(call_id, "\n".join(lines), is_error=False)
        return result

    async def glob(self, pattern: str) -> list[str]:
        call_id = await self._emit_call("Glob", {"pattern": pattern})

        # Confine the pattern to /work before it reaches the container.
        try:
            safe_pattern = _confine(pattern)
        except PathOutsideWorkdirError as exc:
            await self._tool_failure(call_id, str(exc), cause=exc)

        try:
            # Pattern passed as sys.argv[1] — no user input embedded in code string.
            code = (
                "import glob as _g, os, sys; "
                "p = sys.argv[1]; "
                "results = sorted("
                "    os.path.relpath(f, '/work')"
                "    for f in _g.glob('/work/' + p, recursive=True)"
                "    if os.path.isfile(f)"
                "    and not any("
                "        part.startswith('.') or part.startswith('_')"
                "        for part in os.path.relpath(f, '/work').split('/')[:-1]"
                "    )"
                "); print('\\n'.join(results))"
            )
            exec_result = await self._provider.exec(
                self._handle,
                ["python3", "-c", code, safe_pattern],
                timeout_ms=_TOOL_EXEC_TIMEOUT_MS,
            )
        except Exception as exc:
            await self._tool_failure(call_id, _safe_tool_error(exc), cause=exc)

        if exec_result.timed_out:
            await self._tool_failure(call_id, "glob timed out")

        matched = [line for line in exec_result.stdout.splitlines() if line.strip()]
        result_str = "\n".join(matched) if matched else "(no matches)"
        await self._emit_result(call_id, result_str, is_error=False)
        return matched

    async def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> list[GrepMatch]:
        call_id = await self._emit_call(
            "Grep", {"pattern": pattern, "path": path, "glob": glob}
        )

        # Bound + validate the regex and confine the search scope before touching
        # the container.
        if len(pattern) > _GREP_PATTERN_MAX_CHARS:
            await self._tool_failure(
                call_id, f"grep pattern too long (>{_GREP_PATTERN_MAX_CHARS} chars)"
            )
        try:
            re.compile(pattern)
        except re.error as exc:
            await self._tool_failure(call_id, f"invalid grep regex: {exc}", cause=exc)
        try:
            safe_path = _confine(path) if path else ""
            safe_glob = _confine(glob) if glob else "**/*"
        except PathOutsideWorkdirError as exc:
            await self._tool_failure(call_id, str(exc), cause=exc)

        try:
            # All three inputs passed as sys.argv — no user input in code string.
            code = (
                "import re, os, glob as _g, sys; "
                "rx = re.compile(sys.argv[1]); "
                "root = '/work/' + sys.argv[2] if sys.argv[2] else '/work'; "
                "gp = sys.argv[3]; "
                f"cap = {_GREP_MATCH_CAP}; "
                "results = []; "
                "files = sorted(_g.glob(os.path.join(root, gp), recursive=True)); "
                "["
                "  results.append(f'{os.path.relpath(f, \"/work\")}:{i}:{ln.rstrip()}')"
                "  for f in files"
                "  if os.path.isfile(f)"
                "  and not any(p.startswith('.') or p.startswith('_')"
                "              for p in os.path.relpath(f, '/work').split('/')[:-1])"
                "  and len(results) < cap"
                "  for i, ln in enumerate(open(f, encoding='utf-8', errors='replace'), 1)"
                "  if rx.search(ln) and len(results) < cap"
                "]; "
                "print('\\n'.join(results))"
            )
            exec_result = await self._provider.exec(
                self._handle,
                ["python3", "-c", code, pattern, safe_path, safe_glob],
                timeout_ms=_TOOL_EXEC_TIMEOUT_MS,
            )
        except Exception as exc:
            await self._tool_failure(call_id, _safe_tool_error(exc), cause=exc)

        if exec_result.timed_out:
            await self._tool_failure(
                call_id, "grep timed out (pattern may be too expensive — narrow it)"
            )

        matches: list[GrepMatch] = []
        for raw in exec_result.stdout.splitlines():
            if not raw.strip():
                continue
            parts = raw.split(":", 2)
            if len(parts) == 3:
                try:
                    matches.append(GrepMatch(parts[0], int(parts[1]), parts[2]))
                except ValueError:
                    pass

        output = (
            "\n".join(f"{m.path}:{m.line_number}:{m.line}" for m in matches)
            if matches
            else "(no matches)"
        )
        await self._emit_result(call_id, output, is_error=False)
        return matches


__all__ = [
    "SandboxToolset",
    "BashResult",
    "GrepMatch",
    "ToolsetError",
    "NoMatchError",
    "MultipleMatchesError",
    "PathOutsideWorkdirError",
]
