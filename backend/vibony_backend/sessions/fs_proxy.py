"""WebFsProxy: direct sandbox access for the browser candidate runtime.

Backend REST /files handlers and the candidate-web WS route fs_request ops
(tree/read/write) straight to the sandbox provider (docker mode) or a local
tempdir (local mode) — no WS forwarding. This is the only candidate FS path
since the legacy CLI runtime was removed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class WebFsProxy:
    """FS proxy for web sessions.

    Routes tree/read/write ops directly to the sandbox provider (docker mode)
    or to a local tempdir (local mode) — no CLI WS needed.
    """

    def __init__(
        self,
        provider: Any = None,
        handle: Any = None,
        workdir: Path | None = None,
    ) -> None:
        self._provider = provider
        self._handle = handle
        self._workdir = workdir

    async def request(
        self,
        op: str,
        path: str | None = None,
        content: str | None = None,
    ) -> dict[str, Any]:
        """Handle one fs_request op; return dict with response fields."""
        if op == "tree":
            return await self._tree()
        elif op == "read":
            if not path:
                return {"error": "path required for op=read"}
            return await self._read(path)
        elif op == "write":
            if not path:
                return {"error": "path required for op=write"}
            if content is None:
                return {"error": "content required for op=write"}
            return await self._write(path, content)
        else:
            return {"error": f"unknown op: {op!r}"}

    async def _tree(self) -> dict[str, Any]:
        if self._provider is not None:
            try:
                entries = await self._provider.list_files(self._handle)
                return {"op": "tree", "entries": entries}
            except Exception as exc:
                return {"error": str(exc)}
        # local mode
        if self._workdir is not None:
            return {"op": "tree", "entries": _local_list_files(self._workdir)}
        return {"error": "no sandbox available"}

    async def _read(self, path: str) -> dict[str, Any]:
        if self._provider is not None:
            try:
                file_content = await self._provider.read_file(self._handle, path)
                return {"op": "read", "path": path, "content": file_content}
            except FileNotFoundError:
                return {"error": f"not found: {path}"}
            except Exception as exc:
                return {"error": str(exc)}
        # local mode
        if self._workdir is not None:
            try:
                full = _safe_local_path(self._workdir, path)
                file_content = full.read_text(encoding="utf-8", errors="replace")
                return {"op": "read", "path": path, "content": file_content}
            except (FileNotFoundError, ValueError) as exc:
                return {"error": str(exc)}
        return {"error": "no sandbox available"}

    async def _write(self, path: str, content: str) -> dict[str, Any]:
        if self._provider is not None:
            try:
                await self._provider.write_file(self._handle, path, content)
                return {"op": "write", "path": path}
            except Exception as exc:
                return {"error": str(exc)}
        # local mode
        if self._workdir is not None:
            try:
                full = _safe_local_path(self._workdir, path)
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_text(content, encoding="utf-8")
                return {"op": "write", "path": path}
            except (ValueError, OSError) as exc:
                return {"error": str(exc)}
        return {"error": "no sandbox available"}


def _safe_local_path(workdir: Path, path: str) -> Path:
    """Resolve a relative path within workdir; raise ValueError if it escapes."""
    if not path:
        raise ValueError("empty path")
    p = Path(path)
    if p.is_absolute():
        raise ValueError(f"absolute path not allowed: {path!r}")
    full = (workdir / p).resolve()
    try:
        full.relative_to(workdir)
    except ValueError as exc:
        raise ValueError(f"path escapes workdir: {path!r}") from exc
    return full


def _local_list_files(workdir: Path) -> list[dict[str, Any]]:
    """List non-private files and empty dirs in a local directory.

    Non-empty dirs are implied by their file paths; only empty dirs get an
    explicit ``kind="dir"`` entry so a fresh `mkdir` shows in the tree.
    """
    entries: list[dict[str, Any]] = []
    for p in workdir.rglob("*"):
        rel = p.relative_to(workdir)
        parts = rel.parts
        if any(part.startswith(".") or part.startswith("_") for part in parts):
            continue
        if p.is_file():
            if rel.suffix in {".pyc", ".pyo"}:
                continue
            entries.append({"path": str(rel), "kind": "file", "size": p.stat().st_size})
        elif p.is_dir():
            try:
                is_empty = not any(p.iterdir())
            except OSError:
                # Unreadable dir (permissions) — skip rather than 500 the tree.
                continue
            if is_empty:
                entries.append({"path": str(rel), "kind": "dir", "size": 0})
    return sorted(entries, key=lambda e: e["path"])


class WebFsProxyRegistry:
    """Global map of session_code → active WebFsProxy for web sessions."""

    def __init__(self) -> None:
        self._proxies: dict[str, WebFsProxy] = {}

    def register(self, code: str, proxy: WebFsProxy) -> None:
        self._proxies[code] = proxy

    def unregister(self, code: str, proxy: WebFsProxy | None = None) -> None:
        """Drop the proxy for ``code``.

        Pass ``proxy`` to unregister *only if* it is still the current one
        (ownership-keyed). A stale AgentHost teardown that races a reconnect
        must not evict the fresh host's proxy — without the identity
        check the fresh candidate sees "sandbox not ready". ``proxy=None`` keeps
        the old unconditional behaviour for callers with no host reference.
        """
        if proxy is None or self._proxies.get(code) is proxy:
            self._proxies.pop(code, None)

    def get(self, code: str) -> WebFsProxy | None:
        return self._proxies.get(code)


web_fs_proxy_registry = WebFsProxyRegistry()

__all__ = [
    "WebFsProxy",
    "WebFsProxyRegistry",
    "web_fs_proxy_registry",
]
