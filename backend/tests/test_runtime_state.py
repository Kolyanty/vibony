"""Durable control/usage and evidence-integrity regressions (no paid calls)."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vibony_backend.agent_host.host import AgentHost, BackendEventSink
from vibony_backend.agent_host.controller import ControllerHook, TrapControlState
from vibony_backend.db.models import Session, SessionAssessment
from vibony_backend.llm_usage import completion, session_usage, UsageLimitExceeded
from vibony_backend.sessions.runtime_store import load_runtime, save_runtime, PersistenceError

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def runtime_db(db_engine):
    import vibony_backend.db.engine as engine
    async with db_engine.begin() as conn:
        await conn.execute(text("TRUNCATE sessions CASCADE"))
    old = engine._engine, engine._sessionmaker
    engine._engine = db_engine
    engine._sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    sid = uuid.uuid4()
    async with engine._sessionmaker() as db:
        db.add(Session(id=sid, code="STATE-0001", status="active", task_id="demo",
            protocol_version=6, ai_config={"primary_model": "test"}, candidate_token=str(uuid.uuid4()),
            expires_at=datetime.now(UTC) + timedelta(hours=1)))
        await db.commit()
    yield engine._sessionmaker, sid
    async with db_engine.begin() as conn:
        await conn.execute(text("TRUNCATE sessions CASCADE"))
    engine._engine, engine._sessionmaker = old


def host(sid):
    return AgentHost(session_id=sid, session_code="STATE-0001", task_id="demo", ai_config={"primary_model": "test"})


def hook():
    return ControllerHook(model="test", contracts=[], state=TrapControlState(frozenset({1, 2})))


async def test_control_and_budget_survive_host_recreation(runtime_db):
    _, sid = runtime_db
    first = host(sid)
    first._controller = hook()
    first._controller.state.unlocked.add(1)
    first._controller.state.fail_opens = 2
    first._budget.record({"total_tokens": 123})
    await first._checkpoint_runtime()
    second = host(sid)
    async def resolve():
        second._controller = hook()
    with patch.object(second, "_restore_messages", AsyncMock()), patch.object(second, "_resolve_task", resolve), patch.object(second, "_start_local", AsyncMock()):
        await second.start()
    assert second._controller.state.unlocked == {1}
    assert second._controller.state.fail_opens == 2
    assert second._budget.llm_calls == 1
    assert second._budget.total_tokens == 123


async def test_usage_counts_all_roles_and_assessment_separately(runtime_db, monkeypatch):
    sm, _ = runtime_db
    monkeypatch.setenv("VIBONY_SESSION_MAX_LLM_CALLS", "2")
    reply = SimpleNamespace(usage={"total_tokens": 12})
    with patch("litellm.acompletion", AsyncMock(return_value=reply)) as provider:
        with session_usage("STATE-0001"):
            await completion("assistant", model="test")
            await completion("controller", model="test")
        # A new host/context cannot reset the quota.
        with session_usage("STATE-0001"):
            with pytest.raises(UsageLimitExceeded):
                await completion("compaction", model="test")
            await completion("assessment", model="judge")
        assert provider.await_count == 3
    async with sm() as db:
        row = (await db.execute(select(Session))).scalar_one()
        assert row.llm_usage["assistant"]["tokens"] == 12
        assert row.llm_usage["controller"]["calls"] == 1
        assert row.llm_usage["assessment"]["models"] == ["judge"]


async def test_failed_provider_call_still_consumes_quota(runtime_db, monkeypatch):
    monkeypatch.setenv("VIBONY_SESSION_MAX_LLM_CALLS", "1")
    with patch("litellm.acompletion", AsyncMock(side_effect=RuntimeError("offline"))) as provider:
        with session_usage("STATE-0001"):
            with pytest.raises(RuntimeError, match="offline"):
                await completion("controller", model="test")
            with pytest.raises(UsageLimitExceeded):
                await completion("assistant", model="test")
        assert provider.await_count == 1


async def test_interrupted_turn_cannot_resume_as_clean(runtime_db):
    _, sid = runtime_db
    await save_runtime("STATE-0001", {"turn_in_progress": True})
    with pytest.raises(PersistenceError, match="interrupted"):
        await host(sid).start()
    assert "interrupted_turn" in (await load_runtime("STATE-0001"))["integrity_errors"]


async def test_stale_checkpoint_cannot_erase_integrity_error(runtime_db):
    await save_runtime("STATE-0001", {"integrity_errors": ["lost_event"]})
    await save_runtime("STATE-0001", {"controller": {"unlocked": [1]}})
    assert (await load_runtime("STATE-0001"))["integrity_errors"] == ["lost_event"]


@pytest.mark.parametrize("state", [
    {"controller": {"fail_opens": 1}},
    {"controller": {"unresolved_rounds": 1}},
    {"fs_write_in_progress": True},
    {"turn_in_progress": True},
    {"integrity_errors": ["lost_event"]},
])
async def test_assessment_withheld_after_controller_failure(runtime_db, state):
    sm, sid = runtime_db
    from vibony_backend.assessment.job import _insert_pending, _run
    await save_runtime("STATE-0001", state)
    await _insert_pending(sid)
    with patch("vibony_backend.assessment.job.generate_assessment", AsyncMock()) as generate:
        await _run(sid, "STATE-0001")
        generate.assert_not_awaited()
    async with sm() as db:
        row = (await db.execute(select(SessionAssessment))).scalar_one()
        assert row.status == "failed"
        assert "assessment withheld" in row.error


async def test_event_storage_failure_propagates():
    with patch("vibony_backend.agent_host.host.store_event", AsyncMock(side_effect=RuntimeError("offline"))):
        with pytest.raises(PersistenceError):
            await BackendEventSink(uuid.uuid4(), "x").emit("assistant_message", {"text": "x"})


async def test_tool_dispatch_does_not_swallow_persistence_failure():
    from vibony_backend.agent_host.tools import dispatch_tool_call
    toolset = SimpleNamespace(write=AsyncMock(side_effect=PersistenceError("offline")))
    with pytest.raises(PersistenceError):
        await dispatch_tool_call(toolset, {"function": {"name": "Write", "arguments": '{"path":"a", "content":"b"}'}})


async def test_concurrent_roles_cannot_overreserve(runtime_db, monkeypatch):
    import asyncio
    monkeypatch.setenv("VIBONY_SESSION_MAX_LLM_CALLS", "1")
    with patch("litellm.acompletion", AsyncMock(return_value=SimpleNamespace(usage=None))) as provider:
        with session_usage("STATE-0001"):
            results = await asyncio.gather(completion("assistant", model="test"), completion("controller", model="test"), return_exceptions=True)
    assert provider.await_count == 1
    assert sum(isinstance(result, UsageLimitExceeded) for result in results) == 1


async def test_task_snapshot_takes_precedence_over_live_task(runtime_db):
    from dataclasses import asdict
    import json
    from vibony_backend.tasks.store import LoadedTask, resolve_for_session
    sm, sid = runtime_db
    task = LoadedTask(uuid.uuid4(), uuid.uuid4(), "fixed", "Original", "Task", [], "image", {}, {}, "private", False)
    await save_runtime("STATE-0001", {"task": json.loads(json.dumps(asdict(task), default=str))})
    async with sm() as db:
        row = await db.get(Session, sid)
        with patch("vibony_backend.tasks.store._resolve_by_slug", AsyncMock(side_effect=AssertionError("must use snapshot"))):
            assert await resolve_for_session(db, row) == task


async def test_candidate_save_is_not_acknowledged_when_event_is_lost(runtime_db):
    _, sid = runtime_db
    agent = host(sid)
    proxy = SimpleNamespace(request=AsyncMock(return_value={"ok": True}))
    with patch.object(agent._sink, "emit", AsyncMock(side_effect=PersistenceError("offline"))):
        result = await agent.write_candidate_file(proxy, "code.py", "x = 1")
    assert "error" in result
    assert not agent._accepting
    state = await load_runtime("STATE-0001")
    assert "candidate_save_failed" in state["integrity_errors"]
    assert state["fs_write_in_progress"]


async def test_candidate_save_records_event_before_acknowledgement(runtime_db):
    _, sid = runtime_db
    agent = host(sid)
    proxy = SimpleNamespace(request=AsyncMock(return_value={"ok": True}))
    with patch.object(agent._sink, "emit", AsyncMock()) as emit:
        result = await agent.write_candidate_file(proxy, "code.py", "x = 1")
    assert result == {"ok": True}
    emit.assert_awaited_once()
    assert not (await load_runtime("STATE-0001"))["fs_write_in_progress"]


async def test_inline_command_event_failure_marks_evidence_incomplete(runtime_db):
    _, sid = runtime_db
    agent = host(sid)
    with patch.object(agent._sink, "emit", AsyncMock(side_effect=PersistenceError("offline"))):
        with pytest.raises(PersistenceError):
            await agent._run_bash("echo test", "cmd-1", "auto_allow")
    assert not agent._accepting
    state = await load_runtime("STATE-0001")
    assert "command_evidence_failed" in state["integrity_errors"]
    assert state["turn_in_progress"]


async def test_editor_write_is_refused_after_quiesce(runtime_db):
    _, sid = runtime_db
    agent = host(sid)
    agent._accepting = False
    proxy = SimpleNamespace(request=AsyncMock())
    assert "error" in await agent.write_candidate_file(proxy, "a.py", "x = 1")
    proxy.request.assert_not_awaited()


async def test_rejected_editor_write_does_not_emit_file_change(runtime_db):
    _, sid = runtime_db
    agent = host(sid)
    proxy = SimpleNamespace(request=AsyncMock(return_value={"error": "path not allowed"}))
    with patch.object(agent._sink, "emit", AsyncMock()) as emit:
        assert "error" in await agent.write_candidate_file(proxy, "../a.py", "x")
    emit.assert_not_awaited()
    assert not (await load_runtime("STATE-0001"))["fs_write_in_progress"]


async def test_server_default_model_is_checkpointed(runtime_db):
    _, sid = runtime_db
    from vibony_backend.proxy import DEFAULT_MODEL
    agent = AgentHost(session_id=sid, session_code="STATE-0001", task_id="demo", ai_config={})
    with patch.object(agent, "_restore_messages", AsyncMock()), patch.object(agent, "_resolve_task", AsyncMock()), patch.object(agent, "_start_local", AsyncMock()):
        await agent.start()
    assert (await load_runtime("STATE-0001"))["assistant_model"] == DEFAULT_MODEL


async def test_exhausted_shared_quota_preserves_editing_and_assessment(runtime_db, monkeypatch):
    from vibony_backend.agent_host.loop import BUDGET_EXHAUSTED_MESSAGE
    from vibony_backend.assessment.job import _insert_pending, _run
    sm, sid = runtime_db
    monkeypatch.setenv("VIBONY_SESSION_MAX_LLM_CALLS", "2")
    agent = host(sid)
    agent._started = True
    agent._toolset = object()
    await agent._sink.emit("session_start", {"task_id": "demo", "ai_config": {}, "protocol_version": 7, "cli_version": "web"})

    async def call(request):
        return await completion("assistant", model="test")

    with patch("litellm.acompletion", AsyncMock(return_value=SimpleNamespace(usage=None))) as provider:
        with session_usage("STATE-0001"):
            await completion("assistant", model="test")
            await completion("controller", model="test")
        agent._budget.record(None)
        with patch("vibony_backend.agent_host.loop.call_llm", side_effect=call):
            await agent.submit_user_message("continue")
        assert provider.await_count == 2
    assert agent._messages, agent._runtime_state
    assert agent._messages[-1]["content"] == BUDGET_EXHAUSTED_MESSAGE
    assert agent._accepting
    state = await load_runtime("STATE-0001")
    assert state["turn_in_progress"] is False
    assert not state.get("integrity_errors")
    proxy = SimpleNamespace(request=AsyncMock(return_value={"ok": True}))
    assert await agent.write_candidate_file(proxy, "a.py", "x") == {"ok": True}
    await _insert_pending(sid)
    report = ("Done", [], "test")
    with patch("vibony_backend.assessment.job.generate_assessment", AsyncMock(return_value=report)) as generate:
        await _run(sid, "STATE-0001")
        generate.assert_awaited_once()
    async with sm() as db:
        assessment = (await db.execute(select(SessionAssessment))).scalar_one()
        assert assessment.status == "ready", assessment.error


async def test_compaction_quota_does_not_invalidate_completed_turn(runtime_db):
    _, sid = runtime_db
    agent = host(sid)
    agent._started = True
    agent._toolset = object()
    await agent._sink.emit("session_start", {"task_id": "demo", "ai_config": {}, "protocol_version": 7, "cli_version": "web"})
    reply = SimpleNamespace(content="Done", tool_calls=None, usage=None, finish_reason="stop")
    with patch("vibony_backend.agent_host.loop.call_llm", AsyncMock(return_value=reply)), patch(
        "vibony_backend.agent_host.host.compact_messages", AsyncMock(side_effect=UsageLimitExceeded())
    ):
        await agent.submit_user_message("continue")
    assert agent._messages[-1]["content"] == "Done"
    assert agent._accepting
    state = await load_runtime("STATE-0001")
    assert not state["turn_in_progress"]
    assert not state.get("integrity_errors")
