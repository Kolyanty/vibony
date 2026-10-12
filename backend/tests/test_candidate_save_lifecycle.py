"""Editor saves must not block WS control frames or escape terminal draining."""
import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from starlette.websockets import WebSocketDisconnect

from vibony_backend.agent_host.host import AgentHost, MAX_PENDING_FILE_WRITES
from vibony_backend.sessions.ws import _recv_loop_web

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture
def agent():
    host = AgentHost(uuid.uuid4(), "SAVE-TEST", "demo", {})
    host._checkpoint_runtime = AsyncMock()
    host._sink.emit = AsyncMock()
    return host


async def test_autosave_does_not_block_bash_confirmation(agent):
    confirmation = asyncio.get_running_loop().create_future()
    agent._pending_confirms["cmd"] = confirmation
    entered = asyncio.Event()

    async def turn():
        async with agent._turn_lock:
            entered.set()
            assert await confirmation == "allow_once"

    agent._turn_task = asyncio.create_task(turn())
    await entered.wait()
    frames = [
        {"kind": "fs_request", "id": str(i), "op": "write", "path": f"{i}.py", "content": str(i)}
        for i in range(2)
    ] + [{"kind": "bash_confirm_response", "cmd_id": "cmd", "choice": "allow_once"}]
    ws = SimpleNamespace(receive_text=AsyncMock(side_effect=[*(json.dumps(f) for f in frames), WebSocketDisconnect()]), send_json=AsyncMock())
    proxy = SimpleNamespace(request=AsyncMock(return_value={"ok": True}))
    try:
        with patch("vibony_backend.sessions.ws.web_fs_proxy_registry.get", return_value=proxy):
            await asyncio.wait_for(_recv_loop_web(ws, agent), 1)
        await asyncio.wait_for(agent.quiesce(), 1)
        assert confirmation.done()
        assert proxy.request.await_count == 2
        assert [c.args[0]["id"] for c in ws.send_json.await_args_list] == ["0", "1"]
        assert agent._sink.emit.await_count == 2
    finally:
        await agent.terminate()


async def test_quiesce_drains_accepted_save_before_terminal_transition(agent):
    entered, release = asyncio.Event(), asyncio.Event()
    order = []

    async def write(*args, **kwargs):
        entered.set()
        await release.wait()
        order.append("write")
        return {"ok": True}

    async def emit(*args):
        order.append("event")

    async def reply(result):
        assert result == {"ok": True}
        order.append("ack")

    agent._sink.emit = emit
    proxy = SimpleNamespace(request=write)
    assert agent.start_candidate_file_write(proxy, "a.py", "x", reply)
    await entered.wait()
    drain = asyncio.create_task(agent.quiesce())
    await asyncio.sleep(0)
    assert not drain.done()
    assert not agent.start_candidate_file_write(proxy, "b.py", "x", reply)
    release.set()
    await asyncio.wait_for(drain, 1)
    order.append("session_end")
    assert order == ["write", "event", "ack", "session_end"]
    assert agent._runtime_state["fs_write_in_progress"] is False


async def test_write_queue_is_bounded_and_drains_already_accepted_saves(agent):
    proxy = SimpleNamespace(request=AsyncMock(return_value={"ok": True}))
    reply = AsyncMock()
    await agent._turn_lock.acquire()
    for i in range(MAX_PENDING_FILE_WRITES):
        assert agent.start_candidate_file_write(proxy, str(i), "x", reply)
    assert not agent.start_candidate_file_write(proxy, "overflow", "x", reply)
    drain = asyncio.create_task(agent.quiesce())
    await asyncio.sleep(0)
    assert not drain.done()
    agent._turn_lock.release()
    await asyncio.wait_for(drain, 1)
    assert proxy.request.await_count == MAX_PENDING_FILE_WRITES
    assert [c.kwargs["path"] for c in proxy.request.await_args_list] == [str(i) for i in range(MAX_PENDING_FILE_WRITES)]
    assert reply.await_count == MAX_PENDING_FILE_WRITES


@pytest.mark.parametrize("stop", ["quiesce", "terminate"])
async def test_cancelled_save_keeps_incomplete_evidence_marker(agent, stop):
    entered = asyncio.Event()

    async def write(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    reply = AsyncMock()
    assert agent.start_candidate_file_write(SimpleNamespace(request=write), "a.py", "x", reply)
    tasks = list(agent._file_tasks)
    await entered.wait()
    if stop == "quiesce":
        await agent.quiesce(timeout=0.01)
    else:
        await agent.terminate()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert all(t.cancelled() for t in tasks)
    assert agent._runtime_state["fs_write_in_progress"] is True
    reply.assert_not_awaited()
    agent._sink.emit.assert_not_awaited()


@pytest.mark.parametrize("source,target", [
    ("save", "save"), ("save", "turn"), ("save", "bash"),
    ("turn", "save"), ("turn", "bash"), ("bash", "save"), ("bash", "turn"),
])
async def test_queued_operation_cannot_erase_cancelled_operation_marker(agent, source, target):
    entered = asyncio.Event()
    calls = []
    agent._started = True
    agent._toolset = object()
    agent._persist_messages = AsyncMock()

    async def work(kind):
        calls.append(kind)
        if len(calls) == 1:
            entered.set()
            await asyncio.Event().wait()
        return {"ok": True}

    async def write(*args, **kwargs):
        return await work("save")

    async def turn(**kwargs):
        await work("turn")

    async def bash(*args):
        await work("bash")

    def start(kind):
        if kind == "save":
            before = set(agent._file_tasks)
            assert agent.start_candidate_file_write(SimpleNamespace(request=write), "a.py", "x", AsyncMock())
            return (agent._file_tasks - before).pop()
        if kind == "turn":
            assert agent.start_user_message("continue")
            return agent._turn_task
        agent._bash_task = asyncio.create_task(agent._run_bash("echo x", "cmd", "auto_allow"))
        return agent._bash_task

    with patch("vibony_backend.agent_host.host.chat_turn", side_effect=turn), patch.object(
        agent, "_run_bash_locked", side_effect=bash
    ):
        first = start(source)
        await entered.wait()
        second = start(target)
        await asyncio.sleep(0)
        assert not second.done()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await asyncio.wait_for(agent.quiesce(), 1)
    assert calls == [source]
    marker = "fs_write_in_progress" if source == "save" else "turn_in_progress"
    assert agent._runtime_state[marker] is True
    assert not agent._accepting


async def test_quiesce_timeout_does_not_clear_marker_via_second_save(agent):
    entered = asyncio.Event()
    calls = []

    async def write(*args, **kwargs):
        calls.append(kwargs["path"])
        entered.set()
        await asyncio.Event().wait()

    proxy = SimpleNamespace(request=write)
    assert agent.start_candidate_file_write(proxy, "first.py", "x", AsyncMock())
    await entered.wait()
    assert agent.start_candidate_file_write(proxy, "second.py", "x", AsyncMock())
    await asyncio.wait_for(agent.quiesce(timeout=0.01), 1)
    assert calls == ["first.py"]
    assert agent._runtime_state["fs_write_in_progress"] is True
    agent._sink.emit.assert_not_awaited()
