"""LLM tool definitions and dispatcher for AgentHost.

TOOL_DEFINITIONS are protocol artefacts. dispatch_tool_call is duck-typed
against any toolset that has read/write/edit/bash/glob/grep methods.

The dispatcher is shared by the session runtime and its mocked evaluations.
"""

from __future__ import annotations

import json
from vibony_backend.sessions.runtime_store import PersistenceError
from typing import Any

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "Read",
            "description": (
                "Read the file contents as a UTF-8 string. "
                "`offset`/`limit` — line-based pagination (0-based)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the working directory. Absolute paths are not allowed.",
                    },
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    "limit": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "How many lines to read. Optional.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Write",
            "description": (
                "Write the whole file. Creates intermediate directories. "
                "Overwrites without confirmation. For targeted edits use `Edit`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Edit",
            "description": (
                "Replace a specific string in a file. If there are multiple matches "
                "without `replace_all` it returns an error — add more context."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean", "default": False},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Bash",
            "description": (
                "Run a shell command. By default cwd is the working directory. "
                "Returns stdout, stderr, exit_code, duration_ms, timed_out."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string", "description": "Relative path. Optional."},
                    "timeout_ms": {"type": "integer", "minimum": 1, "default": 120000},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Glob",
            "description": "Find files by glob pattern. Returns a sorted list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "For example `**/*.py`."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Grep",
            "description": (
                "Search files with a Python regex. Restrict by directory (`path`) "
                "and glob pattern (`glob`). At most 200 matches."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "Directory to search. Optional."},
                    "glob": {"type": "string", "description": "Glob filter for names. Optional."},
                },
                "required": ["pattern"],
            },
        },
    },
]


async def dispatch_tool_call(toolset: Any, tool_call: dict[str, Any]) -> str:
    """Execute one LLM tool_call and return plaintext result for LLM.

    Accepts any duck-typed toolset with read/write/edit/bash/glob/grep.
    ToolsetError → formatted error string (LLM can recover).
    Other exceptions → also formatted as error strings (avoids crashing the loop).
    """
    func = tool_call.get("function") or {}
    name = func.get("name")
    raw_args = func.get("arguments", "{}")

    if not name:
        return "Error: ToolCallMalformed: missing function.name"

    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    except json.JSONDecodeError as e:
        return f"Error: ToolCallMalformed: invalid JSON arguments: {e}"

    if not isinstance(args, dict):
        return f"Error: ToolCallMalformed: arguments must be object, got {type(args).__name__}"

    handler = _HANDLERS.get(name)
    if handler is None:
        return f"Error: UnknownTool: {name!r} is not a registered tool"

    try:
        return await handler(toolset, args)
    except PersistenceError:
        raise
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


async def _h_read(ts: Any, args: dict[str, Any]) -> str:
    return await ts.read(
        path=args["path"],
        offset=args.get("offset", 0),
        limit=args.get("limit"),
    )


async def _h_write(ts: Any, args: dict[str, Any]) -> str:
    await ts.write(path=args["path"], content=args["content"])
    return "OK"


async def _h_edit(ts: Any, args: dict[str, Any]) -> str:
    await ts.edit(
        path=args["path"],
        old_string=args["old_string"],
        new_string=args["new_string"],
        replace_all=args.get("replace_all", False),
    )
    return "OK"


async def _h_bash(ts: Any, args: dict[str, Any]) -> str:
    result = await ts.bash(
        command=args["command"],
        cwd=args.get("cwd"),
        timeout_ms=args.get("timeout_ms", 120_000),
    )
    lines = [f"exit_code: {result.exit_code}", f"duration_ms: {result.duration_ms}"]
    if result.timed_out:
        lines.append("timed_out: true")
    if result.stdout:
        lines.append(f"stdout:\n{result.stdout}")
    if result.stderr:
        lines.append(f"stderr:\n{result.stderr}")
    return "\n".join(lines)


async def _h_glob(ts: Any, args: dict[str, Any]) -> str:
    paths = await ts.glob(pattern=args["pattern"])
    return "\n".join(paths) if paths else "(no matches)"


async def _h_grep(ts: Any, args: dict[str, Any]) -> str:
    matches = await ts.grep(
        pattern=args["pattern"],
        path=args.get("path"),
        glob=args.get("glob"),
    )
    if not matches:
        return "(no matches)"
    return "\n".join(f"{m.path}:{m.line_number}:{m.line}" for m in matches)


_HANDLERS: dict[str, Any] = {
    "Read": _h_read,
    "Write": _h_write,
    "Edit": _h_edit,
    "Bash": _h_bash,
    "Glob": _h_glob,
    "Grep": _h_grep,
}

__all__ = ["TOOL_DEFINITIONS", "dispatch_tool_call"]
