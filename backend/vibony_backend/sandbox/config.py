"""Env-driven sandbox configuration."""

from __future__ import annotations

import os

def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


# "local" keeps the BackendLocalToolset behaviour — safe default for dev/test.
# Set to "docker" in production to use DockerSandboxProvider.
SANDBOX_PROVIDER: str = os.getenv("VIBONY_SANDBOX_PROVIDER", "local")

# Fail-closed gate. Local mode runs candidate-driven bash UNSANDBOXED
# on the backend host (BackendLocalToolset has no container and no bash gate), so
# production MUST run with VIBONY_SANDBOX_PROVIDER=docker. An AgentHost refuses to
# start in local mode unless this dev/test flag is explicitly set.
ALLOW_LOCAL_SANDBOX: bool = _env_flag("VIBONY_ALLOW_LOCAL_SANDBOX")


def ensure_local_sandbox_allowed(mode: str) -> None:
    """Raise unless ``mode`` is a sandboxed provider or local mode is allowed.

    Reads ``ALLOW_LOCAL_SANDBOX`` from the module at call time so tests can
    monkeypatch it. Called from ``AgentHost.start`` before provisioning.
    """
    if mode != "docker" and not ALLOW_LOCAL_SANDBOX:
        raise RuntimeError(
            "Refusing to start an AgentHost in local sandbox mode: local mode runs "
            "candidate code unsandboxed on the backend host (no container, no bash "
            "gate). Set VIBONY_SANDBOX_PROVIDER=docker for production, or "
            "VIBONY_ALLOW_LOCAL_SANDBOX=1 to explicitly allow local mode (dev/test only)."
        )

SANDBOX_IMAGE: str = os.getenv("VIBONY_SANDBOX_IMAGE", "vibony/sandbox-py:3.12")

SANDBOX_CPU_LIMIT: float = float(os.getenv("VIBONY_SANDBOX_CPU_LIMIT", "1.0"))
SANDBOX_MEM_LIMIT: str = os.getenv("VIBONY_SANDBOX_MEM_LIMIT", "512m")

# Max number of processes/threads inside a sandbox container (fork-bomb guard).
SANDBOX_PIDS_LIMIT: int = int(os.getenv("VIBONY_SANDBOX_PIDS_LIMIT", "256"))
# Max open file descriptors (nofile ulimit, soft == hard).
SANDBOX_NOFILE: int = int(os.getenv("VIBONY_SANDBOX_NOFILE", "1024"))

# Dedicated thread-pool size for blocking Docker SDK calls. A
# timed-out exec leaves its worker thread running (a Python thread can't be
# killed); routing every sandbox SDK call through a dedicated bounded pool keeps
# such leaks contained here instead of saturating asyncio's shared default
# executor that the rest of the backend (DB offloads, etc.) relies on.
SANDBOX_THREAD_POOL_SIZE: int = int(os.getenv("VIBONY_SANDBOX_THREAD_POOL_SIZE", "32"))

# Container hardening. cap_drop=ALL + no-new-privileges +
# private IPC + no swap spill are always on (cheap, no functional cost for the
# candidate's Python/Node workload). Read-only rootfs is OPT-IN: it needs tmpfs
# mounts for the writable workdir and must be verified on a real Linux Docker
# host before enabling in prod (macOS/CI can't exercise it).
SANDBOX_CAP_DROP: list[str] = ["ALL"]
SANDBOX_READONLY_ROOTFS: bool = _env_flag("VIBONY_SANDBOX_READONLY_ROOTFS")
# tmpfs size for the writable /work (and /tmp) mounts when read-only rootfs is on.
SANDBOX_WORK_TMPFS_SIZE: str = os.getenv("VIBONY_SANDBOX_WORK_TMPFS_SIZE", "256m")
SANDBOX_TMP_TMPFS_SIZE: str = os.getenv("VIBONY_SANDBOX_TMP_TMPFS_SIZE", "64m")

# Comma-separated hostnames/CIDRs; full allowlist mechanism deferred to v1.6.
SANDBOX_EGRESS_ALLOWLIST: list[str] = [
    h.strip()
    for h in os.getenv("VIBONY_SANDBOX_EGRESS_ALLOWLIST", "").split(",")
    if h.strip()
]

# OCI runtime for sandbox containers. Unset/empty → Docker default
# (runc). Set to "runsc" in production for gVisor kernel isolation. macOS dev /
# CI cannot run gVisor, so it stays unset there; real escape verification is
# deferred to a Linux host. None is passed through to containers.run
# as "use the daemon default", so an unset value is a no-op.
SANDBOX_RUNTIME: str | None = os.getenv("VIBONY_SANDBOX_RUNTIME") or None

# Docker network mode for sandbox containers. "none" = fully isolated (default).
# Set to "bridge" in dev to allow internet access for package installs.
_ALLOWED_NETWORK_MODES = {"none", "bridge"}
SANDBOX_NETWORK: str = os.getenv("VIBONY_SANDBOX_NETWORK", "none")
if SANDBOX_NETWORK not in _ALLOWED_NETWORK_MODES:
    raise ValueError(
        f"VIBONY_SANDBOX_NETWORK must be one of {_ALLOWED_NETWORK_MODES!r}, "
        f"got {SANDBOX_NETWORK!r}"
    )

# Bash command execution timeout (ms). Default 120 s.
# Raise to 600000 (10 min) when the sandbox needs to install heavy packages (torch etc.).
SANDBOX_BASH_TIMEOUT_MS: int = int(os.getenv("VIBONY_SANDBOX_BASH_TIMEOUT_MS", "120000"))
