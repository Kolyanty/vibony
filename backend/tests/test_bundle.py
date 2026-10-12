"""Bundle builder and GET /sessions/{code}/bundle.tar.gz.

Unit tests for bundle.py:
  - Public files land in the tar.
  - Private files (INTERVIEWER.md, weakness.yaml, _solution.py, .DS_Store) do not.
  - Determinism: two builds produce the same SHA-256.
  - Size > 10 MB → ValueError.
  - get_or_build_bundle caches: the second call does not read the disk (mocked).

Endpoint tests (http_client fixture):
  - GET /sessions/{code}/bundle.tar.gz → 200, gzip, X-Bundle-SHA256 header.
  - bundle_sha256 is saved on the session after the first request.
  - A repeated GET /sessions/{code}/bundle.tar.gz → the same sha256.
  - GET for an unknown code → 404.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from vibony_backend.sessions.bundle import (
    PRIVATE_NAMES,
    MAX_BUNDLE_BYTES,
    _is_private,
    build_bundle,
    build_bundle_from_files,
    get_or_build_bundle,
    invalidate_cache,
)

VALID_PAYLOAD = {
    "task_id": "task-01-training-loop",
    "ai_config": {
        "primary_model": "deepseek/deepseek-chat",
        "fallback_models": [],
    },
}


# ---------------------------------------------------------------------------
# Unit: _is_private
# ---------------------------------------------------------------------------


def test_is_private_fixed_names():
    for name in PRIVATE_NAMES:
        assert _is_private(name), f"expected private: {name}"


def test_is_private_underscore_prefix():
    assert _is_private("_solution.py")
    assert _is_private("_notes.md")
    assert not _is_private("train.py")


def test_is_private_dot_prefix():
    assert _is_private(".DS_Store")
    assert _is_private(".git")
    assert not _is_private("task.md")


# ---------------------------------------------------------------------------
# Unit: build_bundle
# ---------------------------------------------------------------------------


def _make_task_dir(tmp_path: Path, files: dict[str, str]) -> Path:
    task_dir = tmp_path / "task-test"
    task_dir.mkdir()
    for name, content in files.items():
        (task_dir / name).write_text(content, encoding="utf-8")
    return task_dir


def _list_tar_names(tar_bytes: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        return [m.name for m in tar.getmembers()]


def test_build_bundle_includes_public_files(tmp_path: Path):
    task_dir = _make_task_dir(
        tmp_path,
        {"task.md": "# Task", "train.py": "pass", "model.py": "pass"},
    )
    tar_bytes, sha256 = build_bundle(task_dir)

    names = _list_tar_names(tar_bytes)
    assert "task.md" in names
    assert "train.py" in names
    assert "model.py" in names
    assert sha256 and len(sha256) == 64


def test_build_bundle_excludes_private_files(tmp_path: Path):
    task_dir = _make_task_dir(
        tmp_path,
        {
            "task.md": "# Task",
            "INTERVIEWER.md": "secret",
            "weakness.yaml": "secret",
            "persona-playbook.md": "secret",
            "spec.yaml": "secret",
            "_solution.py": "secret",
            ".DS_Store": "junk",
            "train.py": "pass",
        },
    )
    tar_bytes, _ = build_bundle(task_dir)
    names = _list_tar_names(tar_bytes)

    assert "task.md" in names
    assert "train.py" in names
    for private in ["INTERVIEWER.md", "weakness.yaml", "persona-playbook.md", "spec.yaml", "_solution.py", ".DS_Store"]:
        assert private not in names, f"must not be in bundle: {private}"


def test_build_bundle_stable_sha256(tmp_path: Path):
    task_dir = _make_task_dir(tmp_path, {"task.md": "# Task", "model.py": "x=1"})
    _, sha1 = build_bundle(task_dir)
    _, sha2 = build_bundle(task_dir)
    assert sha1 == sha2


def test_build_bundle_gzip_header_mtime_is_zero(tmp_path: Path):
    """The gzip header mtime must be pinned to 0, not wall-clock time.

    Otherwise two rebuilds of identical content seconds apart yield different
    bytes (and bundle_sha256 drifts). The gzip header carries mtime as a
    little-endian uint32 at bytes [4:8] right after magic(2)+method(1)+flags(1).
    A time-dependent build would put a non-zero timestamp there.
    """
    task_dir = _make_task_dir(tmp_path, {"task.md": "# Task"})
    tar_bytes, _ = build_bundle(task_dir)
    assert tar_bytes[:2] == b"\x1f\x8b"  # gzip magic
    assert tar_bytes[4:8] == b"\x00\x00\x00\x00"  # header mtime == 0


def test_build_bundle_stable_ordering(tmp_path: Path):
    """The order of files in the tar does not depend on disk iteration order."""
    task_dir = _make_task_dir(tmp_path, {"z.py": "z", "a.py": "a", "m.py": "m"})
    _, sha1 = build_bundle(task_dir)
    _, sha2 = build_bundle(task_dir)
    assert sha1 == sha2

    # The first file in the archive is a.py (sorted by name)
    tar_bytes, _ = build_bundle(task_dir)
    names = _list_tar_names(tar_bytes)
    assert names == sorted(names)


def test_build_bundle_max_size_exceeded(tmp_path: Path):
    task_dir = tmp_path / "task-big"
    task_dir.mkdir()
    # Use incompressible bytes (all different) to defeat gzip compression.
    # Patch MAX_BUNDLE_BYTES to a small value — no need to allocate 10 MB.
    import os
    small_max = 512
    (task_dir / "data.bin").write_bytes(os.urandom(small_max + 1))

    with patch("vibony_backend.sessions.bundle.MAX_BUNDLE_BYTES", small_max):
        with pytest.raises(ValueError, match="MB"):
            build_bundle(task_dir)


def test_get_or_build_bundle_caches(tmp_path: Path):
    task_dir = _make_task_dir(tmp_path, {"task.md": "# Task"})
    invalidate_cache()

    call_count = 0
    original_build = build_bundle.__wrapped__ if hasattr(build_bundle, "__wrapped__") else None

    with patch("vibony_backend.sessions.bundle.build_bundle", wraps=build_bundle) as mock_build:
        get_or_build_bundle("task-cache-test", task_dir)
        get_or_build_bundle("task-cache-test", task_dir)
        assert mock_build.call_count == 1, "build_bundle must be called exactly once (the second comes from cache)"

    invalidate_cache("task-cache-test")


# ---------------------------------------------------------------------------
# Unit: build_bundle_from_files (DB-backed source)
# ---------------------------------------------------------------------------


def _tar_member(tar_bytes: bytes, name: str) -> str:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        return tar.extractfile(name).read().decode("utf-8")  # type: ignore[union-attr]


def test_build_bundle_from_files_basic():
    files = [
        {"path": "train.py", "content": "pass\n"},
        {"path": "model.py", "content": "x = 1\n"},
    ]
    tar_bytes, sha = build_bundle_from_files(files, description_md="# Brief")
    names = _list_tar_names(tar_bytes)
    assert set(names) == {"train.py", "model.py", "task.md"}
    assert _tar_member(tar_bytes, "task.md") == "# Brief"
    assert len(sha) == 64


def test_build_bundle_from_files_deterministic():
    files = [{"path": "b.py", "content": "b"}, {"path": "a.py", "content": "a"}]
    _, sha1 = build_bundle_from_files(files, description_md="x")
    _, sha2 = build_bundle_from_files(list(reversed(files)), description_md="x")
    assert sha1 == sha2  # ordering-independent


def test_build_bundle_from_files_keeps_explicit_task_md():
    files = [{"path": "task.md", "content": "explicit"}]
    tar_bytes, _ = build_bundle_from_files(files, description_md="synthesized")
    # An explicit task.md wins — description_md is not double-added.
    assert _tar_member(tar_bytes, "task.md") == "explicit"


def test_build_bundle_from_files_skips_malformed_entries():
    files = [
        {"path": "ok.py", "content": "1"},
        {"path": "", "content": "x"},
        {"content": "no path"},
        "not a dict",
    ]
    tar_bytes, _ = build_bundle_from_files(files)  # type: ignore[arg-type]
    assert _list_tar_names(tar_bytes) == ["ok.py"]


def test_build_bundle_from_files_empty_description_no_task_md():
    tar_bytes, _ = build_bundle_from_files([{"path": "x.py", "content": "1"}])
    assert _list_tar_names(tar_bytes) == ["x.py"]


def test_build_bundle_from_files_skips_private_entries():
    """Defense-in-depth: a private filename in starter_files never ships,
    even though the editor writes starter_files straight to the DB."""
    files = [
        {"path": "train.py", "content": "ok"},
        {"path": "weakness.yaml", "content": "secret"},
        {"path": "_solution.py", "content": "secret"},
        {"path": "spec.yaml", "content": "secret"},
        {"path": ".env", "content": "secret"},
    ]
    tar_bytes, _ = build_bundle_from_files(files)
    assert _list_tar_names(tar_bytes) == ["train.py"]


# ---------------------------------------------------------------------------
# Endpoint: GET /sessions/{code}/bundle.tar.gz
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_get_bundle_returns_gzip(authed_client):
    client, _ = authed_client
    r = await client.post("/sessions", json=VALID_PAYLOAD)
    assert r.status_code == 201
    code, token = r.json()["session_code"], r.json()["candidate_token"]

    resp = await client.get(f"/sessions/{code}/bundle.tar.gz?candidate_token={token}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/gzip"
    assert "x-bundle-sha256" in resp.headers

    sha256_header = resp.headers["x-bundle-sha256"]
    assert len(sha256_header) == 64

    actual_sha256 = hashlib.sha256(resp.content).hexdigest()
    assert actual_sha256 == sha256_header


@pytest.mark.asyncio(loop_scope="session")
async def test_get_bundle_saves_sha256_to_session(authed_client):
    client, _ = authed_client
    r = await client.post("/sessions", json=VALID_PAYLOAD)
    code, token = r.json()["session_code"], r.json()["candidate_token"]

    # Before first bundle fetch, bundle_sha256 == None in the GET config response
    config_before = (await client.get(f"/sessions/{code}?candidate_token={token}")).json()
    assert config_before["bundle_sha256"] is None

    await client.get(f"/sessions/{code}/bundle.tar.gz?candidate_token={token}")

    config_after = (await client.get(f"/sessions/{code}?candidate_token={token}")).json()
    assert config_after["bundle_sha256"] is not None
    assert len(config_after["bundle_sha256"]) == 64


@pytest.mark.asyncio(loop_scope="session")
async def test_get_bundle_idempotent_sha256(authed_client):
    client, _ = authed_client
    r = await client.post("/sessions", json=VALID_PAYLOAD)
    code, token = r.json()["session_code"], r.json()["candidate_token"]

    resp1 = await client.get(f"/sessions/{code}/bundle.tar.gz?candidate_token={token}")
    resp2 = await client.get(f"/sessions/{code}/bundle.tar.gz?candidate_token={token}")
    assert resp1.headers["x-bundle-sha256"] == resp2.headers["x-bundle-sha256"]
    assert resp1.content == resp2.content


@pytest.mark.asyncio(loop_scope="session")
async def test_get_bundle_contains_task_files(authed_client):
    client, _ = authed_client
    r = await client.post("/sessions", json=VALID_PAYLOAD)
    code, token = r.json()["session_code"], r.json()["candidate_token"]

    resp = await client.get(f"/sessions/{code}/bundle.tar.gz?candidate_token={token}")
    names = _list_tar_names(resp.content)

    assert "task.md" in names
    assert "train.py" in names
    for private in ["INTERVIEWER.md", "weakness.yaml", "_solution.py", "spec.yaml"]:
        assert private not in names


@pytest.mark.asyncio(loop_scope="session")
async def test_get_bundle_unknown_session_returns_401(http_client: AsyncClient):
    # The candidate_token path returns a uniform 401 for an unknown code (and a
    # wrong token) — it must not be a session-code existence oracle, so no 404 here.
    resp = await http_client.get("/sessions/UNKN-OWNX/bundle.tar.gz?candidate_token=dummy")
    assert resp.status_code == 401
