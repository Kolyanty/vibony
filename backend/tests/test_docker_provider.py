"""Integration tests for DockerSandboxProvider — require Docker daemon + image.

Run with: pytest -m docker backend/tests/test_docker_provider.py

These tests are skipped automatically unless the "docker" mark is requested
AND a Docker daemon is reachable. They use the real Docker SDK to spin up
containers from vibony/sandbox-py:3.12, so the image must be built first:

    docker build -t vibony/sandbox-py:3.12 sandbox/images/py

All containers created here are labelled vibony.session=test-* and are
removed in the test teardown even if the test fails.
"""

from __future__ import annotations

import io
import tarfile
import uuid

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.docker,
]


# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------

def _docker_available() -> bool:
    try:
        import docker
        client = docker.from_env()
        client.ping()
        client.images.get("vibony/sandbox-py:3.12")
        return True
    except Exception:
        return False


skip_no_docker = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker daemon not reachable or vibony/sandbox-py:3.12 image not built",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bundle(seed_content: str = "# seed") -> bytes:
    """Return a tar.gz with a single seed.py file."""
    buf = io.BytesIO()
    data = seed_content.encode()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        ti = tarfile.TarInfo(name="seed.py")
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fixture: provider + auto-cleanup
# ---------------------------------------------------------------------------

@pytest.fixture
async def provider_and_cleanup():
    from vibony_backend.sandbox.docker_provider import DockerSandboxProvider, SandboxHandle

    prov = DockerSandboxProvider()
    created_handles: list[SandboxHandle] = []

    yield prov, created_handles

    # Teardown: remove all containers created in the test.
    for handle in created_handles:
        await prov.teardown(handle)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@skip_no_docker
async def test_create_extracts_bundle(provider_and_cleanup):
    """create() should start a container and extract the task bundle into /work."""
    prov, handles = provider_and_cleanup
    session_code = f"test-{uuid.uuid4().hex[:8]}"

    handle = await prov.create(
        _make_bundle("# hello from test"),
        "work",
        session_code=session_code,
    )
    handles.append(handle)

    assert handle.sandbox_id  # non-empty container ID

    # Seed file should be present in /work.
    result = await prov.exec(handle, "cat /work/seed.py")
    assert result.exit_code == 0
    assert "hello from test" in result.stdout


@skip_no_docker
async def test_exec_captures_stdout_stderr(provider_and_cleanup):
    """exec() should capture stdout, stderr and exit_code."""
    prov, handles = provider_and_cleanup
    handle = await prov.create(_make_bundle(), "work", session_code=f"test-{uuid.uuid4().hex[:8]}")
    handles.append(handle)

    result = await prov.exec(handle, "echo out && echo err >&2 && exit 2")
    assert result.exit_code == 2
    assert "out" in result.stdout
    assert "err" in result.stderr
    assert result.duration_ms >= 0


@skip_no_docker
async def test_write_file_persists(provider_and_cleanup):
    """write_file() should persist content that read_file() can retrieve."""
    prov, handles = provider_and_cleanup
    handle = await prov.create(_make_bundle(), "work", session_code=f"test-{uuid.uuid4().hex[:8]}")
    handles.append(handle)

    await prov.write_file(handle, "answer.py", "x = 42\n")
    content = await prov.read_file(handle, "answer.py")
    assert "x = 42" in content


@skip_no_docker
async def test_read_file_not_found_raises(provider_and_cleanup):
    """read_file() should raise FileNotFoundError for absent paths."""
    prov, handles = provider_and_cleanup
    handle = await prov.create(_make_bundle(), "work", session_code=f"test-{uuid.uuid4().hex[:8]}")
    handles.append(handle)

    with pytest.raises(FileNotFoundError):
        await prov.read_file(handle, "does_not_exist.py")


@skip_no_docker
async def test_teardown_is_idempotent(provider_and_cleanup):
    """teardown() called twice should not raise."""
    prov, _ = provider_and_cleanup  # don't add to auto-cleanup list
    handle = await prov.create(_make_bundle(), "work", session_code=f"test-{uuid.uuid4().hex[:8]}")

    await prov.teardown(handle)
    await prov.teardown(handle)  # must not raise


@skip_no_docker
async def test_reaper_kills_orphans(provider_and_cleanup):
    """list_orphans() returns the IDs of labelled containers; teardown removes them."""
    from vibony_backend.sandbox.reaper import reap_orphans

    prov, handles = provider_and_cleanup
    session_code = f"test-{uuid.uuid4().hex[:8]}"

    handle = await prov.create(_make_bundle(), "work", session_code=session_code)
    # Don't add to auto-cleanup — reaper should handle it.

    orphans_before = await prov.list_orphans()
    assert handle.sandbox_id in orphans_before

    count = await reap_orphans(prov)
    assert count >= 1

    # Container must be gone.
    orphans_after = await prov.list_orphans()
    assert handle.sandbox_id not in orphans_after
