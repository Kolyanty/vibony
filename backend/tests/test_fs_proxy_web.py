"""Tests for WebFsProxy — direct sandbox FS access for the browser candidate.

Four scenarios:
  1. op='tree' against fake docker provider returns expected shape.
  2. op='read' returns file content.
  3. op='write' persists content via provider.
  4. REST /files routes through WebFsProxy.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from vibony_backend.sessions.fs_proxy import WebFsProxy, WebFsProxyRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_docker_provider_fake(
    tree_entries: list[dict] | None = None,
    read_content: str = "content",
    write_error: Exception | None = None,
) -> AsyncMock:
    fake = AsyncMock()
    fake.list_files = AsyncMock(return_value=tree_entries or [
        {"path": "train.py", "kind": "file", "size": 1024},
        {"path": "data.py", "kind": "file", "size": 512},
    ])
    if write_error:
        fake.write_file = AsyncMock(side_effect=write_error)
    else:
        fake.write_file = AsyncMock(return_value=None)
    fake.read_file = AsyncMock(return_value=read_content)
    return fake


# ---------------------------------------------------------------------------
# op='tree' tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_web_fs_proxy_tree_returns_entries() -> None:
    provider = _make_docker_provider_fake()
    handle = object()
    proxy = WebFsProxy(provider=provider, handle=handle)

    result = await proxy.request("tree")

    assert "error" not in result
    assert result["op"] == "tree"
    assert len(result["entries"]) == 2
    assert result["entries"][0]["path"] == "train.py"
    provider.list_files.assert_awaited_once_with(handle)


# ---------------------------------------------------------------------------
# op='read' tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_web_fs_proxy_read_returns_content() -> None:
    provider = _make_docker_provider_fake(read_content="import torch\n")
    handle = object()
    proxy = WebFsProxy(provider=provider, handle=handle)

    result = await proxy.request("read", path="train.py")

    assert "error" not in result
    assert result["op"] == "read"
    assert result["path"] == "train.py"
    assert result["content"] == "import torch\n"
    provider.read_file.assert_awaited_once_with(handle, "train.py")


@pytest.mark.asyncio(loop_scope="session")
async def test_web_fs_proxy_read_missing_path_returns_error() -> None:
    provider = _make_docker_provider_fake()
    proxy = WebFsProxy(provider=provider, handle=object())

    result = await proxy.request("read")
    assert "error" in result


# ---------------------------------------------------------------------------
# op='write' tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_web_fs_proxy_write_persists() -> None:
    provider = _make_docker_provider_fake()
    handle = object()
    proxy = WebFsProxy(provider=provider, handle=handle)

    result = await proxy.request("write", path="train.py", content="new content")

    assert "error" not in result
    assert result["op"] == "write"
    assert result["path"] == "train.py"
    provider.write_file.assert_awaited_once_with(handle, "train.py", "new content")


# ---------------------------------------------------------------------------
# Local (tempdir) mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_web_fs_proxy_local_tree_and_read_write(tmp_path: Path) -> None:
    (tmp_path / "train.py").write_text("import torch\n")
    (tmp_path / "data.py").write_text("import csv\n")
    # Private files should be excluded
    (tmp_path / "_private.py").write_text("secret")
    (tmp_path / ".hidden.py").write_text("hidden")

    proxy = WebFsProxy(workdir=tmp_path)

    tree = await proxy.request("tree")
    assert "error" not in tree
    paths = [e["path"] for e in tree["entries"]]
    assert "train.py" in paths
    assert "data.py" in paths
    assert "_private.py" not in paths
    assert ".hidden.py" not in paths

    read = await proxy.request("read", path="train.py")
    assert read.get("content") == "import torch\n"

    write = await proxy.request("write", path="train.py", content="updated\n")
    assert "error" not in write
    assert (tmp_path / "train.py").read_text() == "updated\n"


# ---------------------------------------------------------------------------
# WebFsProxyRegistry
# ---------------------------------------------------------------------------


def test_web_fs_proxy_registry_register_get_unregister() -> None:
    reg = WebFsProxyRegistry()
    assert reg.get("AAAA") is None

    proxy = WebFsProxy(workdir=Path("/tmp"))
    reg.register("AAAA", proxy)
    assert reg.get("AAAA") is proxy

    reg.unregister("AAAA")
    assert reg.get("AAAA") is None


# ---------------------------------------------------------------------------
# REST /files → WebFsProxy
# ---------------------------------------------------------------------------


def test_rest_list_files_web_session_uses_web_proxy(
    db_engine,  # noqa: ANN001
    database_url: str,
) -> None:
    """GET /sessions/{code}/files routes through WebFsProxy."""
    import vibony_backend.db.engine as eng
    from vibony_backend.main import app
    from starlette.testclient import TestClient
    from vibony_backend.sessions.fs_proxy import web_fs_proxy_registry

    eng._engine = None
    eng._sessionmaker = None

    with TestClient(app) as client:
        # Create a web session
        client.post(
            "/auth/signup",
            json={
                "email": f"fstest_{uuid.uuid4()}@example.com",
                "password": "password123",
                "company_name": "FsTest Co",
            },
        )
        resp = client.post(
            "/sessions",
            json={
                "task_id": "task-01-training-loop",
                "ai_config": {
                    "primary_model": "deepseek/deepseek-chat",
                    "fallback_models": [],
                },
            },
        )
        assert resp.status_code == 201
        code = resp.json()["session_code"]

        # Register a fake WebFsProxy
        fake_proxy = WebFsProxy.__new__(WebFsProxy)
        fake_proxy._provider = None
        fake_proxy._handle = None
        fake_proxy._workdir = None

        async def _fake_request(op: str, path: str | None = None, content: str | None = None) -> dict:
            return {"op": "tree", "entries": [{"path": "mock.py", "kind": "file", "size": 100}]}

        fake_proxy.request = _fake_request  # type: ignore[method-assign]
        web_fs_proxy_registry.register(code, fake_proxy)

        try:
            files_resp = client.get(f"/sessions/{code}/files")
            assert files_resp.status_code == 200
            entries = files_resp.json()["entries"]
            assert any(e["path"] == "mock.py" for e in entries)
        finally:
            web_fs_proxy_registry.unregister(code)

    eng._engine = None
    eng._sessionmaker = None
