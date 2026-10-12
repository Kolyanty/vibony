"""Resource limits + per-task runtime_image (mocked Docker client).

These run without a Docker daemon: the docker client is a MagicMock, so we only
assert the kwargs threaded into containers.run and the spec.yaml parse.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# This file mixes sync pure-function tests with async ones,
# so it deliberately has no module-level pytestmark (that would mark the sync
# tests asyncio and spam warnings). The async tests are decorated individually
# with loop_scope="session" — the SAME scope every other async/db test file uses.
# Load-bearing: a function-scoped async test creates and tears down its own event
# loop, leaving the session-scoped asyncpg connections (db_engine) bound to a
# now-dead loop and breaking the NEXT db test with "got Future attached to a
# different loop". See tests/test_agent_host_compaction.py for the same note.
_session_loop = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# DockerSandboxProvider.create — limit kwargs
# ---------------------------------------------------------------------------

def _provider_with_fake_client() -> tuple[object, MagicMock]:
    fake_container = MagicMock()
    fake_container.id = "c" * 64
    fake_client = MagicMock()
    fake_client.containers.run.return_value = fake_container
    with patch("docker.from_env", return_value=fake_client):
        from vibony_backend.sandbox.docker_provider import DockerSandboxProvider

        prov = DockerSandboxProvider()
    return prov, fake_client


@_session_loop
async def test_create_passes_pids_nofile_and_image():
    prov, fake_client = _provider_with_fake_client()

    handle = await prov.create(
        b"bundle-bytes",
        "work",
        session_code="S-1",
        image="vibony/sandbox-py:3.12",
        cpu_limit=2.0,
        mem_limit="1g",
        pids_limit=128,
        nofile=2048,
    )

    assert handle.sandbox_id == "c" * 64
    call = fake_client.containers.run.call_args
    assert call.args[0] == "vibony/sandbox-py:3.12"
    assert call.kwargs["pids_limit"] == 128
    assert call.kwargs["mem_limit"] == "1g"
    assert call.kwargs["cpu_quota"] == 200_000  # 2.0 * 100_000
    ulimits = call.kwargs["ulimits"]
    assert len(ulimits) == 1
    assert ulimits[0]["Name"] == "nofile"
    assert ulimits[0]["Soft"] == 2048
    assert ulimits[0]["Hard"] == 2048


@_session_loop
async def test_create_without_nofile_passes_no_ulimits():
    prov, fake_client = _provider_with_fake_client()

    await prov.create(b"bundle", "work", session_code="S-2")

    call = fake_client.containers.run.call_args
    assert call.kwargs["ulimits"] is None
    assert call.kwargs["pids_limit"] is None


@_session_loop
async def test_create_falls_back_to_default_image_when_none():
    prov, fake_client = _provider_with_fake_client()
    from vibony_backend.sandbox.config import SANDBOX_IMAGE

    await prov.create(b"bundle", "work", session_code="S-3", image=None)

    assert fake_client.containers.run.call_args.args[0] == SANDBOX_IMAGE


# ---------------------------------------------------------------------------
# DockerSandboxProvider.create — gVisor runtime kwarg
# ---------------------------------------------------------------------------

@_session_loop
async def test_create_threads_runtime_when_set(monkeypatch):
    # create() does `from .config import ... SANDBOX_RUNTIME` at call time, so
    # patching the config attribute is observed on the next create().
    monkeypatch.setattr(
        "vibony_backend.sandbox.config.SANDBOX_RUNTIME", "runsc", raising=False
    )
    prov, fake_client = _provider_with_fake_client()

    await prov.create(b"bundle", "work", session_code="S-4")

    assert fake_client.containers.run.call_args.kwargs["runtime"] == "runsc"


@_session_loop
async def test_create_runtime_none_by_default(monkeypatch):
    # Unset → None is passed through to containers.run as "daemon default" (runc).
    monkeypatch.setattr(
        "vibony_backend.sandbox.config.SANDBOX_RUNTIME", None, raising=False
    )
    prov, fake_client = _provider_with_fake_client()

    await prov.create(b"bundle", "work", session_code="S-5")

    assert fake_client.containers.run.call_args.kwargs["runtime"] is None

# Note: `read_runtime_image` (sessions/task_spec.py) was removed — the
# per-task image now comes from the DB task (tasks.runtime_image), extracted at
# seed time. Its unit tests retired with the module.


# ---------------------------------------------------------------------------
# DockerSandboxProvider.create — container hardening kwargs
# ---------------------------------------------------------------------------

@_session_loop
async def test_create_passes_hardening_kwargs():
    prov, fake_client = _provider_with_fake_client()

    await prov.create(b"bundle", "work", session_code="H-1", mem_limit="512m")

    kw = fake_client.containers.run.call_args.kwargs
    assert kw["cap_drop"] == ["ALL"]
    assert kw["security_opt"] == ["no-new-privileges:true"]
    assert kw["ipc_mode"] == "private"
    # memswap_limit == mem_limit disables swap spill.
    assert kw["memswap_limit"] == "512m"


@_session_loop
async def test_create_readonly_rootfs_off_by_default():
    prov, fake_client = _provider_with_fake_client()

    await prov.create(b"bundle", "work", session_code="H-2")

    kw = fake_client.containers.run.call_args.kwargs
    assert kw["read_only"] is False
    assert kw["tmpfs"] is None


@_session_loop
async def test_create_readonly_rootfs_mounts_tmpfs_when_enabled(monkeypatch):
    # create() imports the config flags at call time, so patching the module
    # attribute is observed on the next create().
    monkeypatch.setattr(
        "vibony_backend.sandbox.config.SANDBOX_READONLY_ROOTFS", True, raising=False
    )
    prov, fake_client = _provider_with_fake_client()

    await prov.create(b"bundle", "work", session_code="H-3")

    kw = fake_client.containers.run.call_args.kwargs
    assert kw["read_only"] is True
    # Writable tmpfs for the workdir + /tmp so the candidate can still write.
    assert "/work" in kw["tmpfs"]
    assert "/tmp" in kw["tmpfs"]


# ---------------------------------------------------------------------------
# Fail-closed local-sandbox guard
# ---------------------------------------------------------------------------

def test_ensure_local_sandbox_allowed_docker_never_raises(monkeypatch):
    from vibony_backend.sandbox import config

    monkeypatch.setattr(config, "ALLOW_LOCAL_SANDBOX", False, raising=False)
    # docker mode is sandboxed → allowed regardless of the flag.
    config.ensure_local_sandbox_allowed("docker")


def test_ensure_local_sandbox_allowed_local_blocked_without_flag(monkeypatch):
    from vibony_backend.sandbox import config

    monkeypatch.setattr(config, "ALLOW_LOCAL_SANDBOX", False, raising=False)
    with pytest.raises(RuntimeError, match="unsandboxed"):
        config.ensure_local_sandbox_allowed("local")


def test_ensure_local_sandbox_allowed_local_ok_with_flag(monkeypatch):
    from vibony_backend.sandbox import config

    monkeypatch.setattr(config, "ALLOW_LOCAL_SANDBOX", True, raising=False)
    config.ensure_local_sandbox_allowed("local")  # must not raise


@_session_loop
async def test_write_file_tar_uses_nonroot_uid():
    """write_file must emit uid/gid 1000 so the non-root container user can edit
    agent-written files — matches bundle.py's tar ownership."""
    import io
    import tarfile

    prov, fake_client = _provider_with_fake_client()
    fake_container = MagicMock()
    fake_client.containers.get.return_value = fake_container

    from vibony_backend.sandbox.docker_provider import SandboxHandle

    await prov.write_file(SandboxHandle(sandbox_id="c" * 64), "sub/answer.py", "x=1\n")

    buf = fake_container.put_archive.call_args.args[1]
    buf.seek(0)
    with tarfile.open(fileobj=io.BytesIO(buf.read()), mode="r") as tf:
        members = {m.name: m for m in tf.getmembers()}
    # every entry (file + parent dir) owned by uid/gid 1000
    assert members, "expected tar members"
    for m in members.values():
        assert m.uid == 1000 and m.gid == 1000, f"{m.name} not uid 1000"


# ---------------------------------------------------------------------------
# Dedicated sandbox thread pool
# ---------------------------------------------------------------------------

def test_sandbox_executor_is_dedicated_and_bounded():
    """Blocking Docker SDK calls run on a dedicated bounded pool, not asyncio's
    shared default executor — so threads leaked by timed-out execs are contained."""
    from concurrent.futures import ThreadPoolExecutor

    from vibony_backend.sandbox import docker_provider as dp
    from vibony_backend.sandbox.config import SANDBOX_THREAD_POOL_SIZE

    assert isinstance(dp._sandbox_executor, ThreadPoolExecutor)
    assert dp._sandbox_executor._max_workers == SANDBOX_THREAD_POOL_SIZE


@_session_loop
async def test_run_in_sandbox_thread_uses_dedicated_pool():
    import threading

    from vibony_backend.sandbox import docker_provider as dp

    name = await dp._run_in_sandbox_thread(lambda: threading.current_thread().name)
    assert name.startswith("vibony-sandbox")
