"""Tests for sandbox FS recovery via event-log replay.

Two layers:

1. ``replay_file_state`` — the projection over file_changed events. Tested
   against the real Postgres fixture: insert a synthetic session + events,
   call the function, assert the per-path projection.

2. ``AgentHost._restore_fs_from_events`` — the side-effecting code that
   applies the projection to a sandbox. Tested with mocked provider so we
   don't need Docker.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vibony_backend.db.models import Event
from vibony_backend.db.models import Session as DbSession
from vibony_backend.sessions.event_store import replay_file_state

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def projection_db(db_engine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Truncate tables, reset the module sessionmaker to use the test engine, and
    yield a sessionmaker that the test can use for commits.

    Required because ``replay_file_state`` opens its own session via the module
    sessionmaker; data written inside an unrelated rolled-back transaction
    fixture wouldn't be visible to it.
    """
    import vibony_backend.db.engine as eng

    async with db_engine.begin() as conn:
        await conn.execute(text(
            "TRUNCATE TABLE events, sessions, memberships, oauth_identities, "
            "invites, users, companies RESTART IDENTITY CASCADE"
        ))

    # Force the module to use our test engine.
    eng._engine = db_engine
    eng._sessionmaker = async_sessionmaker(
        db_engine, expire_on_commit=False, class_=AsyncSession
    )

    yield eng._sessionmaker

    async with db_engine.begin() as conn:
        await conn.execute(text(
            "TRUNCATE TABLE events, sessions, memberships, oauth_identities, "
            "invites, users, companies RESTART IDENTITY CASCADE"
        ))

    eng._engine = None
    eng._sessionmaker = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed(
    sm: async_sessionmaker[AsyncSession],
    code: str,
    events: list[tuple[str, dict[str, Any]]],
) -> uuid.UUID:
    """Create a session row + events with sequential seq, commit, return session id."""
    sid = uuid.uuid4()
    async with sm() as db:
        db.add(
            DbSession(
                id=sid,
                code=code,
                status="active",
                task_id="task-01-training-loop",
                protocol_version=6,
                ai_config={"primary_model": "test"},
                candidate_token=str(uuid.uuid4()),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        for seq, (event_type, payload) in enumerate(events, start=1):
            db.add(
                Event(
                    session_id=sid,
                    seq=seq,
                    id=uuid.uuid4(),
                    type=event_type,
                    client_timestamp=datetime.now(UTC),
                    payload=payload,
                )
            )
        await db.commit()
    return sid


def _file_changed(
    path: str,
    content: str | None,
    op: str = "modified",
    *,
    source: str = "manual",
    is_dir: bool = False,
) -> dict[str, Any]:
    p: dict[str, Any] = {
        "file_path": path,
        "diff": None,
        "source": source,
        "content": content,
        "op": op,
    }
    if is_dir:
        p["is_dir"] = True
    return p


# ---------------------------------------------------------------------------
# replay_file_state — projection tests
# ---------------------------------------------------------------------------


async def test_replay_file_state_empty_session_returns_empty(projection_db) -> None:
    code = "RPLY-0001"
    await _seed(projection_db, code, [])
    assert await replay_file_state(code) == {}


async def test_replay_file_state_latest_content_wins(projection_db) -> None:
    code = "RPLY-0002"
    await _seed(
        projection_db,
        code,
        [
            ("session_start", {"task_id": "t", "ai_config": {}}),
            ("file_changed", _file_changed("train.py", "v1")),
            ("file_changed", _file_changed("train.py", "v2")),
            ("file_changed", _file_changed("notes.md", "hello")),
        ],
    )
    assert await replay_file_state(code) == {"train.py": "v2", "notes.md": "hello"}


async def test_replay_file_state_deletion_marks_none(projection_db) -> None:
    code = "RPLY-0003"
    await _seed(
        projection_db,
        code,
        [
            ("session_start", {}),
            ("file_changed", _file_changed("data.py", "code")),
            ("file_changed", _file_changed("data.py", None, op="deleted")),
        ],
    )
    assert await replay_file_state(code) == {"data.py": None}


async def test_replay_file_state_recreate_after_delete(projection_db) -> None:
    code = "RPLY-0004"
    await _seed(
        projection_db,
        code,
        [
            ("session_start", {}),
            ("file_changed", _file_changed("a.txt", "first")),
            ("file_changed", _file_changed("a.txt", None, op="deleted")),
            ("file_changed", _file_changed("a.txt", "second")),
        ],
    )
    assert await replay_file_state(code) == {"a.txt": "second"}


async def test_replay_file_state_skips_directory_events(projection_db) -> None:
    code = "RPLY-0005"
    await _seed(
        projection_db,
        code,
        [
            ("session_start", {}),
            (
                "file_changed",
                _file_changed("subdir", None, op="created", is_dir=True),
            ),
            ("file_changed", _file_changed("subdir/a.txt", "x")),
        ],
    )
    # The directory event must not appear; the file inside it must.
    assert await replay_file_state(code) == {"subdir/a.txt": "x"}


async def test_replay_file_state_skips_binary_without_content(projection_db) -> None:
    code = "RPLY-0006"
    await _seed(
        projection_db,
        code,
        [
            ("session_start", {}),
            # Old-CLI / binary event: no content key — we cannot reconstruct it.
            (
                "file_changed",
                {"file_path": "image.png", "diff": None, "source": "agent", "op": "modified"},
            ),
            ("file_changed", _file_changed("readme.md", "ok")),
        ],
    )
    assert await replay_file_state(code) == {"readme.md": "ok"}


async def test_replay_file_state_skips_baseline_events(projection_db) -> None:
    """Baseline events are the scaffold put_archive already restores;
    they must not appear in the projection. An untouched file (baseline only)
    is absent; an edited file is keyed to its edit content, not the baseline."""
    code = "RPLY-0007"
    await _seed(
        projection_db,
        code,
        [
            ("session_start", {}),
            ("file_changed", _file_changed("train.py", "scaffold\n", op="created", source="baseline")),
            ("file_changed", _file_changed("data.py", "untouched\n", op="created", source="baseline")),
            ("file_changed", _file_changed("train.py", "edited\n")),
        ],
    )
    assert await replay_file_state(code) == {"train.py": "edited\n"}


# ---------------------------------------------------------------------------
# _restore_fs_from_events — sandbox side-effect tests
# ---------------------------------------------------------------------------


def _make_host(code: str = "TEST-FSRC"):
    from vibony_backend.agent_host.host import AgentHost

    return AgentHost(
        session_id=uuid.uuid4(),
        session_code=code,
        task_id="task-01-training-loop",
        ai_config={"primary_model": "test-model"},
    )


def _attach_fake_sandbox(host) -> tuple[AsyncMock, AsyncMock, MagicMock]:
    """Wire up host with a mock provider and capture emits."""
    write = AsyncMock()
    exec_ = AsyncMock()
    provider = MagicMock(write_file=write, exec=exec_)
    handle = MagicMock(sandbox_id="deadbeefcafebabe")
    host._sandbox_provider = provider
    host._sandbox_handle = handle

    emitted: list[tuple[str, dict[str, Any]]] = []

    async def fake_emit(event_type: str, payload: dict[str, Any]) -> None:
        emitted.append((event_type, payload))

    host._sink = MagicMock()
    host._sink.emit = fake_emit
    host._sink.emitted = emitted  # type: ignore[attr-defined]
    return write, exec_, host._sink


async def test_restore_fs_noop_when_provider_unset() -> None:
    host = _make_host()
    host._sandbox_provider = None
    host._sandbox_handle = None
    sink = MagicMock()
    sink.emit = AsyncMock()
    host._sink = sink

    await host._restore_fs_from_events()
    sink.emit.assert_not_awaited()


async def test_restore_fs_noop_on_empty_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _make_host()
    write, exec_, sink = _attach_fake_sandbox(host)

    async def empty_projection(_code: str) -> dict[str, str | None]:
        return {}

    monkeypatch.setattr(
        "vibony_backend.agent_host.host.replay_file_state", empty_projection
    )

    await host._restore_fs_from_events()
    write.assert_not_awaited()
    exec_.assert_not_awaited()
    assert sink.emitted == []  # type: ignore[attr-defined]


async def test_restore_fs_applies_writes_and_deletions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _make_host()
    write, exec_, sink = _attach_fake_sandbox(host)

    async def projection(_code: str) -> dict[str, str | None]:
        return {
            "train.py": "import torch\n",
            "notes.md": "TODO",
            "data.py": None,
        }

    monkeypatch.setattr(
        "vibony_backend.agent_host.host.replay_file_state", projection
    )

    await host._restore_fs_from_events()

    # Two writes, one delete.
    assert write.await_count == 2
    written_paths = {call.args[1] for call in write.await_args_list}
    assert written_paths == {"train.py", "notes.md"}
    assert exec_.await_count == 1
    assert exec_.await_args.args[1] == ["rm", "-rf", "--", "data.py"]

    # Restore event emitted with accurate counts.
    assert sink.emitted == [  # type: ignore[attr-defined]
        ("sandbox_restored", {"file_count": 2, "deleted_count": 1}),
    ]


async def test_reset_sandbox_does_not_replay_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manual reset must land on clean baseline — never re-apply the event log."""
    host = _make_host()
    monkeypatch.setenv("VIBONY_SANDBOX_PROVIDER", "docker")

    host._teardown_sandbox = AsyncMock()  # type: ignore[method-assign]
    provision = AsyncMock()
    host._provision_docker = provision  # type: ignore[method-assign]

    emitted: list[tuple[str, dict[str, Any]]] = []

    async def fake_emit(event_type: str, payload: dict[str, Any]) -> None:
        emitted.append((event_type, payload))

    host._sink = MagicMock()
    host._sink.emit = fake_emit

    await host.reset_sandbox()

    provision.assert_awaited_once_with(restore=False)
    assert ("sandbox_reset", {}) in emitted


async def test_reset_sandbox_reemits_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manual reset re-seeds File History baselines so post-reset edits diff
    against clean scaffold. The re-emitted baseline must precede sandbox_reset."""
    import io
    import tarfile

    host = _make_host()
    monkeypatch.setenv("VIBONY_SANDBOX_PROVIDER", "docker")

    # A real reset provision re-extracts the bundle and re-populates _bundle_tar;
    # emulate that here since _provision_docker is mocked.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"scaffold\n"
        info = tarfile.TarInfo(name="train.py")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    bundle = buf.getvalue()

    host._teardown_sandbox = AsyncMock()  # type: ignore[method-assign]

    async def fake_provision(*, restore: bool = True) -> None:
        host._bundle_tar = bundle

    host._provision_docker = fake_provision  # type: ignore[method-assign]

    emitted: list[tuple[str, dict[str, Any]]] = []

    async def fake_emit(event_type: str, payload: dict[str, Any]) -> None:
        emitted.append((event_type, payload))

    host._sink = MagicMock()
    host._sink.emit = fake_emit

    await host.reset_sandbox()

    types = [t for t, _ in emitted]
    # A baseline file_changed for the scaffold file is re-emitted...
    baseline = next(
        (p for t, p in emitted if t == "file_changed" and p.get("source") == "baseline"),
        None,
    )
    assert baseline is not None
    assert baseline["file_path"] == "train.py"
    assert baseline["content"] == "scaffold\n"
    # ...before the sandbox_reset marker, so the diff chain restarts cleanly.
    assert types.index("file_changed") < types.index("sandbox_reset")


async def test_restore_fs_skips_path_traversal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crafted event paths that escape /work must never reach write/exec argv."""
    host = _make_host()
    write, exec_, sink = _attach_fake_sandbox(host)

    async def projection(_code: str) -> dict[str, str | None]:
        return {
            "../etc/passwd": None,  # traversal delete — must be skipped
            "/abs/escape.py": "boom",  # absolute write — must be skipped
            "train.py": "ok",  # the one legitimate path
        }

    monkeypatch.setattr(
        "vibony_backend.agent_host.host.replay_file_state", projection
    )

    await host._restore_fs_from_events()

    # Only the safe path is applied; traversal/absolute paths never reach exec.
    assert write.await_count == 1
    assert write.await_args.args[1] == "train.py"
    exec_.assert_not_awaited()
    assert sink.emitted == [  # type: ignore[attr-defined]
        ("sandbox_restored", {"file_count": 1, "deleted_count": 0}),
    ]


async def test_restore_fs_continues_on_per_file_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _make_host()
    provider = MagicMock()
    handle = MagicMock()
    host._sandbox_provider = provider
    host._sandbox_handle = handle

    async def bad_write(_handle, path: str, _content: str) -> None:
        if path == "broken.txt":
            raise RuntimeError("invalid path")

    provider.write_file = AsyncMock(side_effect=bad_write)
    provider.exec = AsyncMock()

    emitted: list[tuple[str, dict[str, Any]]] = []

    async def fake_emit(event_type: str, payload: dict[str, Any]) -> None:
        emitted.append((event_type, payload))

    host._sink = MagicMock()
    host._sink.emit = fake_emit

    async def projection(_code: str) -> dict[str, str | None]:
        return {"good.py": "ok", "broken.txt": "boom", "also-good.md": "yes"}

    monkeypatch.setattr(
        "vibony_backend.agent_host.host.replay_file_state", projection
    )

    await host._restore_fs_from_events()

    assert provider.write_file.await_count == 3
    # Only the two successes are reported.
    assert emitted == [("sandbox_restored", {"file_count": 2, "deleted_count": 0})]


async def test_restore_fs_swallows_replay_query_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _make_host()
    write, exec_, sink = _attach_fake_sandbox(host)

    async def broken(_code: str) -> dict[str, str | None]:
        raise RuntimeError("DB unavailable")

    monkeypatch.setattr(
        "vibony_backend.agent_host.host.replay_file_state", broken
    )

    # Must not raise.
    await host._restore_fs_from_events()

    write.assert_not_awaited()
    exec_.assert_not_awaited()
    assert sink.emitted == []  # type: ignore[attr-defined]
