"""Backend-side LocalToolset for AgentHost.

Minimal local-filesystem toolset for backend use. No bash permission gate,
no fuzzy edit, no snapshot store — those are later concerns.

Emits tool_call / tool_result events to the injected EventSink.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ---------- Result types (mirror of the toolset protocol result types) ----------


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


class ToolsetError(Exception):
    pass


class NoMatchError(ToolsetError):
    pass


class MultipleMatchesError(ToolsetError):
    pass


class PathOutsideWorkdirError(ToolsetError):
    pass


_BASH_OUTPUT_CAP = 1024 * 1024
_GREP_MATCH_CAP = 200
_TRUNC_SUFFIX = "\n... [truncated]"


def _is_private(name: str) -> bool:
    return name.startswith(".") or name.startswith("_")


class BackendLocalToolset:
    """Simple LocalToolset backed by tempdir. Used by AgentHost."""

    def __init__(self, event_sink: Any, workdir: Path) -> None:
        self._sink = event_sink
        self._workdir = Path(workdir).resolve()

    # ---------- internal ----------

    def _resolve(self, path: str) -> Path:
        if not path:
            raise PathOutsideWorkdirError(f"empty path: {path!r}")
        p = Path(path)
        if p.is_absolute():
            raise PathOutsideWorkdirError(f"absolute path not allowed: {path!r}")
        full = (self._workdir / p).resolve()
        try:
            full.relative_to(self._workdir)
        except ValueError as exc:
            raise PathOutsideWorkdirError(f"path escapes workdir: {path!r}") from exc
        return full

    async def _emit_call(
        self,
        tool_name: str,
        input_: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> str:
        tool_call_id = uuid.uuid4().hex
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

    # ---------- public API ----------

    async def read(self, path: str, offset: int = 0, limit: int | None = None) -> str:
        call_id = await self._emit_call("Read", {"path": path, "offset": offset, "limit": limit})
        try:
            full = self._resolve(path)
            if not full.exists():
                raise ToolsetError(f"FileNotFound: {path!r}")
            lines = full.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            sliced = lines[offset:] if limit is None else lines[offset: offset + limit]
            result = "".join(sliced)
        except ToolsetError as exc:
            await self._emit_result(call_id, str(exc), is_error=True)
            raise
        await self._emit_result(call_id, result, is_error=False)
        return result

    async def write(self, path: str, content: str) -> None:
        call_id = await self._emit_call("Write", {"path": path})
        try:
            full = self._resolve(path)
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding="utf-8")
        except ToolsetError as exc:
            await self._emit_result(call_id, str(exc), is_error=True)
            raise
        await self._emit_result(call_id, "OK", is_error=False)

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
            full = self._resolve(path)
            if not full.exists():
                raise ToolsetError(f"FileNotFound: {path!r}")
            text = full.read_text(encoding="utf-8", errors="replace")
            count = text.count(old_string)
            if count == 0:
                raise NoMatchError(f"No match for old_string in {path!r}")
            if count > 1 and not replace_all:
                raise MultipleMatchesError(
                    f"{count} matches for old_string in {path!r}; use replace_all=true"
                )
            updated = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
            full.write_text(updated, encoding="utf-8")
        except ToolsetError as exc:
            await self._emit_result(call_id, str(exc), is_error=True)
            raise
        await self._emit_result(call_id, "OK", is_error=False)

    async def bash(
        self,
        command: str,
        cwd: str | None = None,
        timeout_ms: int = 120_000,
    ) -> BashResult:
        call_id = await self._emit_call("Bash", {"command": command, "cwd": cwd})
        if cwd:
            candidate_cwd = (self._workdir / cwd).resolve()
            try:
                candidate_cwd.relative_to(self._workdir)
            except ValueError:
                err = f"cwd escapes workdir: {cwd!r}"
                await self._emit_result(call_id, err, is_error=True)
                raise PathOutsideWorkdirError(err)
            effective_cwd = candidate_cwd
        else:
            effective_cwd = self._workdir
        timeout_s = timeout_ms / 1000.0
        t0 = time.monotonic()
        timed_out = False
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=effective_cwd,
            )
            try:
                raw_out, raw_err = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_s
                )
            except asyncio.TimeoutError:
                timed_out = True
                proc.kill()
                raw_out, raw_err = await proc.communicate()
        except Exception as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            await self._emit_result(call_id, f"Error: {exc}", is_error=True)
            raise ToolsetError(str(exc)) from exc

        duration_ms = int((time.monotonic() - t0) * 1000)
        stdout = raw_out.decode("utf-8", errors="replace")
        stderr = raw_err.decode("utf-8", errors="replace")
        if len(stdout) > _BASH_OUTPUT_CAP:
            stdout = stdout[:_BASH_OUTPUT_CAP] + _TRUNC_SUFFIX
        if len(stderr) > _BASH_OUTPUT_CAP:
            stderr = stderr[:_BASH_OUTPUT_CAP] + _TRUNC_SUFFIX

        result = BashResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=proc.returncode or 0,
            duration_ms=duration_ms,
            timed_out=timed_out,
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
        try:
            matched = sorted(
                str(p.relative_to(self._workdir))
                for p in self._workdir.glob(pattern)
                if p.is_file()
                and not any(_is_private(part) for part in p.relative_to(self._workdir).parts[:-1])
            )
        except Exception as exc:
            await self._emit_result(call_id, f"Error: {exc}", is_error=True)
            raise ToolsetError(str(exc)) from exc
        result = "\n".join(matched) if matched else "(no matches)"
        await self._emit_result(call_id, result, is_error=False)
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
        try:
            regex = re.compile(pattern)
            search_root = (
                (self._workdir / path).resolve() if path else self._workdir
            )
            file_pattern = glob or "**/*"
            matches: list[GrepMatch] = []
            for file in sorted(search_root.glob(file_pattern)):
                if not file.is_file():
                    continue
                rel = file.relative_to(self._workdir)
                if any(_is_private(part) for part in rel.parts[:-1]):
                    continue
                try:
                    for lineno, line in enumerate(
                        file.read_text(encoding="utf-8", errors="replace").splitlines(), 1
                    ):
                        if regex.search(line):
                            matches.append(GrepMatch(str(rel), lineno, line))
                            if len(matches) >= _GREP_MATCH_CAP:
                                break
                except OSError:
                    continue
                if len(matches) >= _GREP_MATCH_CAP:
                    break
        except re.error as exc:
            await self._emit_result(call_id, f"Error: {exc}", is_error=True)
            raise ToolsetError(str(exc)) from exc

        if matches:
            output = "\n".join(f"{m.path}:{m.line_number}:{m.line}" for m in matches)
        else:
            output = "(no matches)"
        await self._emit_result(call_id, output, is_error=False)
        return matches


__all__ = [
    "BackendLocalToolset",
    "BashResult",
    "GrepMatch",
    "ToolsetError",
    "NoMatchError",
    "MultipleMatchesError",
    "PathOutsideWorkdirError",
]
