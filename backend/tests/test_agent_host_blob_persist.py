"""Persist + restore the LLM message blob.

Two layers:

1. ``save_agent_messages`` / ``load_agent_messages`` — the storage round-trip,
   tested against the real Postgres fixture: seed a session, save a blob with a
   tool round, load it back verbatim.

2. ``AgentHost`` — a completed turn persists the live ``_messages`` blob, and a
   fresh host on the same code restores it verbatim (incl. a tool-call round),
   marking the session resumed. A session with no blob falls back to the legacy
   user/assistant event replay.

The DB-backed tests reset the module sessionmaker to the test engine (same
pattern as test_agent_host_fs_recovery.projection_db) so the functions, which
open their own session via the module sessionmaker, see committed rows.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vibony_backend.db.models import Session as DbSession
from vibony_backend.sessions.event_store import (
    load_agent_messages,
    save_agent_messages,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


# A blob with a full tool-call round — the case the lossy event replay drops.
_BLOB_WITH_TOOL_ROUND = [
    {"role": "user", "content": "list the files"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "bash", "arguments": '{"command": "ls"}'},
            }
        ],
    },
    {"role": "tool", "tool_call_id": "call_1", "content": "train.py\ndata.py\n"},
    {"role": "assistant", "content": "There are two files: train.py and data.py."},
]


@pytest_asyncio.fixture(loop_scope="session")
async def blob_db(db_engine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Truncate tables, point the module sessionmaker at the test engine, yield it."""
    import vibony_backend.db.engine as eng

    async with db_engine.begin() as conn:
        await conn.execute(text(
            "TRUNCATE TABLE events, sessions, memberships, oauth_identities, "
            "invites, users, companies RESTART IDENTITY CASCADE"
        ))

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


async def _seed_session(sm: async_sessionmaker[AsyncSession], code: str) -> uuid.UUID:
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
        await db.commit()
    return sid


def _make_host(code: str):
    from vibony_backend.agent_host.host import AgentHost

    return AgentHost(
        session_id=uuid.uuid4(),
        session_code=code,
        task_id="task-01-training-loop",
        ai_config={"primary_model": "test-model"},
    )


# ---------------------------------------------------------------------------
# Storage round-trip
# ---------------------------------------------------------------------------


async def test_load_returns_none_for_unsaved_session(blob_db) -> None:
    await _seed_session(blob_db, "BLOB-0001")
    assert await load_agent_messages("BLOB-0001") is None


async def test_load_returns_none_for_missing_session(blob_db) -> None:
    assert await load_agent_messages("BLOB-NOPE") is None


async def test_save_then_load_round_trips_tool_round(blob_db) -> None:
    await _seed_session(blob_db, "BLOB-0002")
    await save_agent_messages("BLOB-0002", _BLOB_WITH_TOOL_ROUND)
    loaded = await load_agent_messages("BLOB-0002")
    assert loaded == _BLOB_WITH_TOOL_ROUND
    # The tool round (assistant tool_calls + role:tool result) survives verbatim.
    assert loaded[1]["tool_calls"][0]["id"] == "call_1"
    assert loaded[2]["role"] == "tool"


async def test_save_overwrites_previous_blob(blob_db) -> None:
    await _seed_session(blob_db, "BLOB-0003")
    await save_agent_messages("BLOB-0003", [{"role": "user", "content": "first"}])
    await save_agent_messages("BLOB-0003", _BLOB_WITH_TOOL_ROUND)
    assert await load_agent_messages("BLOB-0003") == _BLOB_WITH_TOOL_ROUND


# ---------------------------------------------------------------------------
# AgentHost: persist after a turn + restore on a fresh host
# ---------------------------------------------------------------------------


async def test_turn_persists_blob_then_fresh_host_restores_it(blob_db) -> None:
    """End-to-end: host A runs a tool-round turn → blob persisted; host B on the
    same code restores it verbatim and is marked resumed (welcome suppressed)."""
    code = "BLOB-0004"
    await _seed_session(blob_db, code)

    host_a = _make_host(code)
    host_a._started = True
    host_a._toolset = object()  # truthy — submit_user_message only checks non-None

    async def fake_chat_turn(toolset, task_id, messages, user_text, **kwargs):
        # Simulate a turn that includes a tool-call round.
        messages.append({"role": "user", "content": user_text})
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command": "ls"}'},
                    }
                ],
            }
        )
        messages.append(
            {"role": "tool", "tool_call_id": "call_1", "content": "train.py\n"}
        )
        messages.append({"role": "assistant", "content": "Found train.py."})

    with (
        patch("vibony_backend.agent_host.host.store_event", new_callable=AsyncMock, return_value=1),
        patch("vibony_backend.agent_host.host.chat_turn", side_effect=fake_chat_turn),
    ):
        await host_a.submit_user_message("list the files")

    # Blob is now in the DB and includes the tool round.
    persisted = await load_agent_messages(code)
    assert persisted is not None
    assert any(m["role"] == "tool" for m in persisted)

    # Fresh host (simulated backend restart) restores the blob verbatim.
    host_b = _make_host(code)
    await host_b._restore_messages()

    assert host_b._messages == host_a._messages
    assert any(m["role"] == "tool" for m in host_b._messages)
    assert host_b._is_resumed is True, "a restored blob means the session is resumed"


async def test_failed_turn_does_not_persist_partial_blob(blob_db) -> None:
    """A turn that raises mid-loop must not overwrite the last-good blob."""
    code = "BLOB-0005"
    await _seed_session(blob_db, code)
    await save_agent_messages(code, _BLOB_WITH_TOOL_ROUND)  # last-good blob

    host = _make_host(code)
    host._started = True
    host._toolset = object()

    async def boom(toolset, task_id, messages, user_text, **kwargs):
        messages.append({"role": "user", "content": user_text})
        raise RuntimeError("LLM exploded mid-turn")

    with (
        patch("vibony_backend.agent_host.host.store_event", new_callable=AsyncMock, return_value=1),
        patch("vibony_backend.agent_host.host.chat_turn", side_effect=boom),
    ):
        await host.submit_user_message("trigger failure")

    # Blob is untouched — the last-good blob from before the failed turn.
    assert await load_agent_messages(code) == _BLOB_WITH_TOOL_ROUND


async def test_legacy_session_without_blob_falls_back_to_event_replay() -> None:
    """No blob (legacy session) → _restore_messages uses replay_messages_for_agent."""
    legacy_history = [
        {"role": "user", "content": "old session"},
        {"role": "assistant", "content": "from the event log"},
    ]
    host = _make_host("BLOB-0006")

    with (
        patch("vibony_backend.agent_host.host.load_agent_messages", new_callable=AsyncMock, return_value=None),
        patch("vibony_backend.agent_host.host.replay_messages_for_agent", new_callable=AsyncMock, return_value=legacy_history),
        patch("vibony_backend.agent_host.host.has_assistant_message", new_callable=AsyncMock, return_value=True),
    ):
        await host._restore_messages()

    assert host._messages == legacy_history
    assert host._is_resumed is True


async def test_blob_path_skips_legacy_replay() -> None:
    """When a blob exists, replay_messages_for_agent must not be consulted."""
    host = _make_host("BLOB-0007")
    replay = AsyncMock(return_value=[{"role": "user", "content": "should not be used"}])

    with (
        patch("vibony_backend.agent_host.host.load_agent_messages", new_callable=AsyncMock, return_value=_BLOB_WITH_TOOL_ROUND),
        patch("vibony_backend.agent_host.host.replay_messages_for_agent", replay),
    ):
        await host._restore_messages()

    assert host._messages == _BLOB_WITH_TOOL_ROUND
    assert host._is_resumed is True
    replay.assert_not_awaited()
