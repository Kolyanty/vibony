"""DockerSandboxProvider — single-host Docker backend for web sessions.

Implements the SandboxProvider protocol from docs/protocol/sandbox-provider.md.
All Docker SDK calls are synchronous; we run them on a dedicated bounded thread
pool (``_run_in_sandbox_thread``) so the FastAPI event loop stays
unblocked and threads leaked by timed-out execs don't saturate asyncio's shared
default executor.

Containers are labelled "vibony.session=<session_code>" so the reaper can find
and remove orphans left by previous backend runs.

Network is isolated (network_mode="none") by default.
Full egress allowlist mechanism deferred to v1.6.

The OCI runtime is the daemon default (runc) unless VIBONY_SANDBOX_RUNTIME is set
to "runsc" (gVisor) for kernel isolation in production.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tarfile
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TypeVar

import posixpath

from .config import SANDBOX_THREAD_POOL_SIZE

logger = logging.getLogger(__name__)

_BASH_OUTPUT_CAP = 1024 * 1024  # 1 MB
_SNAPSHOT_CAP = 500  # max files/dirs returned by snapshot_paths / snapshot_dirs

# Dedicated bounded thread pool for blocking Docker SDK calls. Unlike
# asyncio.to_thread (which uses the shared default executor), threads leaked by a
# timed-out exec saturate only this pool — the rest of the backend's to_thread
# offloads keep running. Threads spawn lazily on first submit, so importing this
# module without ever touching Docker is free.
_sandbox_executor = ThreadPoolExecutor(
    max_workers=SANDBOX_THREAD_POOL_SIZE, thread_name_prefix="vibony-sandbox"
)

_T = TypeVar("_T")


async def _run_in_sandbox_thread(fn: Callable[[], _T]) -> _T:
    """Run a blocking Docker SDK call on the dedicated sandbox thread pool."""
    return await asyncio.get_running_loop().run_in_executor(_sandbox_executor, fn)


def _is_private_sandbox_path(rel: str) -> bool:
    """True if any path component is private (mirrors CLI's is_private_rel)."""
    for part in rel.split("/"):
        if part.startswith(".") or part.startswith("_"):
            return True
    return rel.endswith(".pyc") or rel.endswith(".pyo")


def _safe_path(path: str) -> str:
    """Normalise a relative path and raise ValueError if it escapes /work."""
    norm = posixpath.normpath(path)
    if norm.startswith("..") or posixpath.isabs(norm):
        raise ValueError(f"Path escapes working directory: {path!r}")
    return norm


@dataclass
class SandboxHandle:
    sandbox_id: str  # Docker container ID


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    timed_out: bool


class DockerSandboxProvider:
    """Single-host DockerSandboxProvider over /var/run/docker.sock or DOCKER_HOST."""

    _LABEL_KEY = "vibony.session"
    WORKDIR = "/work"

    def __init__(self) -> None:
        import docker  # lazy — avoids import-time failure when docker pkg missing

        self._client = docker.from_env()

    # ------------------------------------------------------------------
    # SandboxProvider interface
    # ------------------------------------------------------------------

    async def create(
        self,
        task_bundle: bytes,
        task_dir_name: str,
        *,
        session_code: str = "",
        image: str | None = None,
        cpu_limit: float = 1.0,
        mem_limit: str = "512m",
        pids_limit: int | None = None,
        nofile: int | None = None,
    ) -> SandboxHandle:
        import docker  # lazy — docker.types.Ulimit for the nofile limit

        from .config import (
            SANDBOX_CAP_DROP,
            SANDBOX_IMAGE,
            SANDBOX_NETWORK,
            SANDBOX_READONLY_ROOTFS,
            SANDBOX_RUNTIME,
            SANDBOX_TMP_TMPFS_SIZE,
            SANDBOX_WORK_TMPFS_SIZE,
        )

        _image = image or SANDBOX_IMAGE
        ulimits = (
            [docker.types.Ulimit(name="nofile", soft=nofile, hard=nofile)]
            if nofile is not None
            else None
        )
        # Read-only rootfs needs writable tmpfs for the workdir + /tmp.
        # Opt-in (default off) — verify on a Linux host before enabling in prod.
        tmpfs = (
            {
                self.WORKDIR: f"rw,size={SANDBOX_WORK_TMPFS_SIZE}",
                "/tmp": f"rw,size={SANDBOX_TMP_TMPFS_SIZE}",
            }
            if SANDBOX_READONLY_ROOTFS
            else None
        )

        def _run() -> str:
            container = self._client.containers.run(
                _image,
                detach=True,
                labels={self._LABEL_KEY: session_code},
                cpu_period=100_000,
                cpu_quota=int(cpu_limit * 100_000),
                mem_limit=mem_limit,
                # memswap_limit == mem_limit disables swap spill so the
                # advertised memory budget is the real ceiling, not 2x via swap.
                memswap_limit=mem_limit,
                pids_limit=pids_limit,
                ulimits=ulimits,
                # Hardening: drop all caps, block privilege
                # escalation, private IPC namespace. gVisor (runtime) is the
                # primary boundary when enabled; these are defense-in-depth.
                cap_drop=SANDBOX_CAP_DROP,
                security_opt=["no-new-privileges:true"],
                ipc_mode="private",
                read_only=SANDBOX_READONLY_ROOTFS,
                tmpfs=tmpfs,
                network_mode=SANDBOX_NETWORK,
                # None = daemon default (runc); "runsc" = gVisor in prod.
                runtime=SANDBOX_RUNTIME,
                working_dir=self.WORKDIR,
            )
            buf = io.BytesIO(task_bundle)
            container.put_archive(self.WORKDIR, buf)
            return container.id

        container_id = await _run_in_sandbox_thread(_run)
        logger.info("Created sandbox %s for session %s", container_id[:12], session_code)
        return SandboxHandle(sandbox_id=container_id)

    async def exec(
        self,
        handle: SandboxHandle,
        command: str | list[str],
        cwd: str | None = None,
        timeout_ms: int = 120_000,
    ) -> ExecResult:
        """Run a shell command (str) or direct exec (list) inside the sandbox."""
        cmd = ["sh", "-c", command] if isinstance(command, str) else list(command)
        effective_cwd = cwd or self.WORKDIR

        def _run() -> tuple[int, bytes, bytes]:
            container = self._client.containers.get(handle.sandbox_id)
            ec, output = container.exec_run(
                cmd,
                workdir=effective_cwd,
                demux=True,
                stdout=True,
                stderr=True,
            )
            stdout_b, stderr_b = output if isinstance(output, tuple) else (output, b"")
            return (0 if ec is None else ec), stdout_b or b"", stderr_b or b""

        t0 = time.monotonic()
        timed_out = False
        try:
            exit_code, raw_out, raw_err = await asyncio.wait_for(
                _run_in_sandbox_thread(_run),
                timeout=timeout_ms / 1000.0,
            )
        except asyncio.TimeoutError:
            timed_out = True
            exit_code = -1
            raw_out, raw_err = b"", b"timed out"

        duration_ms = int((time.monotonic() - t0) * 1000)
        stdout = raw_out.decode("utf-8", errors="replace")
        stderr = raw_err.decode("utf-8", errors="replace")
        if len(stdout) > _BASH_OUTPUT_CAP:
            stdout = stdout[:_BASH_OUTPUT_CAP] + "\n... [truncated]"
        if len(stderr) > _BASH_OUTPUT_CAP:
            stderr = stderr[:_BASH_OUTPUT_CAP] + "\n... [truncated]"

        return ExecResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration_ms=duration_ms,
            timed_out=timed_out,
        )

    async def exec_stream(
        self,
        handle: SandboxHandle,
        command: str | list[str],
        on_chunk: Callable[[bytes], Awaitable[None]],
        *,
        cwd: str | None = None,
        timeout_ms: int = 120_000,
        tty: bool = True,
    ) -> ExecResult:
        """Execute command and call on_chunk for each output chunk as it arrives.

        Uses TTY mode by default so ANSI colours pass through and stdout/stderr
        are merged into one stream (same as the candidate's terminal).
        Returns ExecResult with accumulated stdout.
        """
        cmd = ["sh", "-c", command] if isinstance(command, str) else list(command)
        effective_cwd = cwd or self.WORKDIR
        timeout_s = timeout_ms / 1000.0

        loop = asyncio.get_running_loop()
        # asyncio.Queue: producer puts items from the Docker thread via
        # loop.call_soon_threadsafe; consumer awaits them here.
        q: asyncio.Queue[bytes | int | Exception] = asyncio.Queue()

        def _producer() -> None:
            try:
                exec_id = self._client.api.exec_create(
                    handle.sandbox_id,
                    cmd,
                    workdir=effective_cwd,
                    tty=tty,
                )
                output_gen = self._client.api.exec_start(exec_id, stream=True, tty=tty)
                for chunk in output_gen:
                    if isinstance(chunk, bytes):
                        loop.call_soon_threadsafe(q.put_nowait, chunk)
                inspect_result = self._client.api.exec_inspect(exec_id)
                ec = inspect_result.get("ExitCode")
                loop.call_soon_threadsafe(q.put_nowait, ec if isinstance(ec, int) else -1)
            except Exception as exc:
                loop.call_soon_threadsafe(q.put_nowait, exc)

        t0 = time.monotonic()
        stdout_parts: list[bytes] = []
        timed_out = False
        exit_code = -1

        producer_task = loop.run_in_executor(_sandbox_executor, _producer)

        try:
            deadline = loop.time() + timeout_s
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    item = await asyncio.wait_for(q.get(), timeout=min(remaining, 30.0))
                except asyncio.TimeoutError:
                    timed_out = True
                    break

                if isinstance(item, Exception):
                    raise item
                elif isinstance(item, int):
                    exit_code = item
                    break
                else:  # bytes chunk
                    stdout_parts.append(item)
                    await on_chunk(item)
        finally:
            # Wait for the producer thread to finish cleanly (max 5s to avoid hangs).
            try:
                await asyncio.wait_for(producer_task, timeout=5.0)
            except Exception:
                pass

        duration_ms = int((time.monotonic() - t0) * 1000)
        stdout = b"".join(stdout_parts).decode("utf-8", errors="replace")
        if len(stdout) > _BASH_OUTPUT_CAP:
            stdout = stdout[:_BASH_OUTPUT_CAP] + "\n... [truncated]"

        return ExecResult(
            stdout=stdout,
            stderr="",
            exit_code=exit_code,
            duration_ms=duration_ms,
            timed_out=timed_out,
        )

    async def read_file(self, handle: SandboxHandle, path: str) -> str:
        safe = _safe_path(path)
        result = await self.exec(handle, ["cat", f"{self.WORKDIR}/{safe}"])
        # A timeout (exit_code -1, timed_out=True) is not a missing file — surface
        # it distinctly so the caller doesn't report "file not found" for a file
        # that exists. Transport errors raised by exec() propagate
        # as-is (and are sanitised by the toolset).
        if result.timed_out:
            raise TimeoutError(f"Read timed out in sandbox: {path!r}")
        if result.exit_code != 0:
            raise FileNotFoundError(f"File not found in sandbox: {path!r}")
        return result.stdout

    async def write_file(
        self, handle: SandboxHandle, path: str, content: str
    ) -> None:
        safe = _safe_path(path)

        def _write() -> None:
            container = self._client.containers.get(handle.sandbox_id)
            data = content.encode("utf-8")
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tf:
                # uid/gid 1000 = the non-root sandbox user: put_archive
                # runs as the daemon (root), so without this the agent's Write/Edit
                # would land root-owned files in a uid-1000 /work that the candidate
                # process can't edit. Matches bundle.py's tar ownership.
                # Add parent dir entry so Docker creates it if absent.
                dirname = os.path.dirname(safe)
                if dirname:
                    di = tarfile.TarInfo(name=dirname)
                    di.type = tarfile.DIRTYPE
                    di.mode = 0o755
                    di.uid = di.gid = 1000
                    tf.addfile(di)
                ti = tarfile.TarInfo(name=safe)
                ti.size = len(data)
                ti.mode = 0o644
                ti.uid = ti.gid = 1000
                tf.addfile(ti, io.BytesIO(data))
            buf.seek(0)
            container.put_archive(self.WORKDIR, buf)

        await _run_in_sandbox_thread(_write)

    async def list_changed_since(
        self, handle: SandboxHandle, since_ts: float
    ) -> list[str]:
        """Return relative paths of non-private files modified after since_ts.

        Applies a 1-second buffer to tolerate clock skew between host and
        container. Uses Python inside the container so no timezone issues.
        """
        _CAP = 50
        code = (
            "import os, sys; "
            "ts = float(sys.argv[1]) - 1; "  # 1s clock-skew buffer
            f"cap = {_CAP}; "
            "changed = []; "
            "["
            "    changed.append(os.path.relpath(os.path.join(r, f), '/work'))"
            "    for r, _dirs, files in os.walk('/work')"
            "    for f in files"
            "    if os.path.getmtime(os.path.join(r, f)) > ts and len(changed) < cap"
            "]; "
            "print('\\n'.join(sorted(changed[:cap])))"
        )
        result = await self.exec(handle, ["python3", "-c", code, str(since_ts)])
        paths = []
        for line in result.stdout.splitlines():
            rel = line.strip()
            if rel and "..." not in rel and not _is_private_sandbox_path(rel):
                paths.append(rel)
        return paths

    async def snapshot_paths(self, handle: SandboxHandle) -> set[str]:
        """Return a snapshot of all non-private file paths currently in the sandbox.

        Used to detect deletions by diffing before/after snapshots around a bash
        command.  Returns only file paths (not directories).
        """
        code = (
            "import os; "
            f"cap = {_SNAPSHOT_CAP}; "
            "paths = []; "
            "["
            "    paths.append(os.path.relpath(os.path.join(r, f), '/work'))"
            "    for r, _dirs, files in os.walk('/work')"
            "    for f in files"
            "    if len(paths) < cap"
            "]; "
            "print('\\n'.join(sorted(paths[:cap])))"
        )
        result = await self.exec(handle, ["python3", "-c", code])
        paths: set[str] = set()
        for line in result.stdout.splitlines():
            rel = line.strip()
            if rel and "..." not in rel and not _is_private_sandbox_path(rel):
                paths.add(rel)
        return paths

    async def snapshot_dirs(self, handle: SandboxHandle) -> set[str]:
        """Return a snapshot of all non-private directory paths currently in the sandbox.

        Excludes the root (/work itself). Used to detect mkdir/rmdir.
        """
        code = (
            "import os; "
            f"cap = {_SNAPSHOT_CAP}; "
            "dirs = []; "
            "["
            "    dirs.append(os.path.relpath(os.path.join(r, d), '/work'))"
            "    for r, subdirs, _files in os.walk('/work')"
            "    for d in subdirs"
            "    if len(dirs) < cap"
            "]; "
            "print('\\n'.join(sorted(dirs[:cap])))"
        )
        result = await self.exec(handle, ["python3", "-c", code])
        dirs: set[str] = set()
        for line in result.stdout.splitlines():
            rel = line.strip()
            if rel and "..." not in rel and not _is_private_sandbox_path(rel):
                dirs.add(rel)
        return dirs

    async def list_files(self, handle: SandboxHandle) -> list[dict]:
        """List non-private files and empty dirs in the sandbox workdir.

        Returns [{path, kind, size}]. Non-empty directories are implied by their
        file paths and are not emitted; only empty directories (e.g. a fresh
        `mkdir`) get an explicit ``kind="dir"`` entry so they show in the tree.
        """
        # One find pass: files prefixed with "f", empty dirs with "d". A line whose
        # leading field is neither is dropped below — this also absorbs the trailing
        # "... [truncated]" marker if stdout hits _BASH_OUTPUT_CAP on huge workdirs.
        result = await self.exec(
            handle,
            f"find {self.WORKDIR} -mindepth 1 "
            f"\\( -type f -printf 'f\\t%P\\t%s\\n' \\) -o "
            f"\\( -type d -empty -printf 'd\\t%P\\n' \\)",
        )
        entries: list[dict] = []
        for raw_line in result.stdout.splitlines():
            raw_line = raw_line.rstrip("\n")
            if not raw_line:
                continue
            kind, _, rest = raw_line.partition("\t")
            if kind == "f":
                # rest = "%P\t%s"; rsplit protects size from tabs in the path.
                parts = rest.rsplit("\t", 1)
                rel = parts[0]
                try:
                    size = int(parts[1]) if len(parts) == 2 else 0
                except ValueError:
                    size = 0
                if not rel or _is_private_sandbox_path(rel):
                    continue
                entries.append({"path": rel, "kind": "file", "size": size})
            elif kind == "d":
                rel = rest
                if not rel or _is_private_sandbox_path(rel):
                    continue
                entries.append({"path": rel, "kind": "dir", "size": 0})
        return sorted(entries, key=lambda e: e["path"])

    async def teardown(self, handle: SandboxHandle) -> None:
        def _remove() -> None:
            try:
                container = self._client.containers.get(handle.sandbox_id)
                container.remove(force=True)
                logger.info("Removed sandbox container %s", handle.sandbox_id[:12])
            except Exception as exc:
                # Container may already be gone — teardown is idempotent.
                logger.debug("teardown %s: %s", handle.sandbox_id[:12], exc)

        await _run_in_sandbox_thread(_remove)

    # ------------------------------------------------------------------
    # Reaper helper
    # ------------------------------------------------------------------

    async def list_orphans(self) -> list[str]:
        """Return IDs of containers labelled vibony.session=* (orphans)."""

        def _list() -> list[str]:
            containers = self._client.containers.list(
                all=True,
                filters={"label": self._LABEL_KEY},
            )
            return [c.id for c in containers]

        return await _run_in_sandbox_thread(_list)


__all__ = ["DockerSandboxProvider", "ExecResult", "SandboxHandle"]
