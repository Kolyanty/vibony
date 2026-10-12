"""AgentHost concurrency & lifecycle races.

Each test reproduces one race the QA sweep flagged. These are written *first* (red
against the pre-fix code) and encode the fixed contract:

  - grace-timer kills a reconnected session / 1 h grace leak /
    fs-proxy of a fresh host unregistered by a stale teardown.
  - terminal transition (submit/timeout/interviewer-end) drops the
    in-flight turn's trailing events → quiesce drains the turn first.
  - candidate inline-bash runs concurrently with an agent turn →
    wrong file_changed provenance → serialise via _turn_lock.
  - reset_sandbox vs terminate() share no lock → reset rebuilds a
    sandbox the terminate tears down.
  - the same quiesce also covers the turn finishing before enqueue.
  - fire-and-forget presence broadcasts are retained until done.

store_event + task/blob loaders are mocked so no DB / LLM is required (mirrors
test_agent_host_concurrency / test_agent_host_reconnect).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from unittest.mock import AsyncMock, patch

import pytest



def _make_host(session_code: str = "RACE-0001"):
    from vibony_backend.agent_host.host import AgentHost

    return AgentHost(
        session_id=uuid.uuid4(),
        session_code=session_code,
        task_id="task-01-training-loop",
        ai_config={"primary_model": "test-model"},
    )


async def _fake_store(sid, code, t, p, **kw):
    return 1


@contextlib.contextmanager
def _start_patches():
    """Patches that let host.start() run in local mode with no DB / LLM."""
    with (
        patch("vibony_backend.agent_host.host.store_event", side_effect=_fake_store),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
        patch(
            "vibony_backend.agent_host.host.load_agent_messages",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "vibony_backend.agent_host.host.replay_messages_for_agent",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "vibony_backend.agent_host.host.has_assistant_message",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        yield


# ---------------------------------------------------------------------------
# registry grace lifecycle (epoch-guarded)
# ---------------------------------------------------------------------------


async def test_overlapping_reconnect_keeps_live_host():
    """A stale connection_closed running *after* a reconnect must not schedule a
    grace timer that kills the live, reconnected session."""
    import vibony_backend.agent_host.host as host_module
    from vibony_backend.agent_host.host import AgentHostRegistry

    orig = host_module.DISCONNECT_GRACE_SECONDS
    host_module.DISCONNECT_GRACE_SECONDS = 0  # would fire immediately
    try:
        reg = AgentHostRegistry()
        sid = uuid.uuid4()
        cfg = {"primary_model": "test-model"}

        host, created, epoch1 = await reg.get_or_create(
            "RCN-0001", sid, "task-01-training-loop", cfg
        )
        assert created is True
        # Reconnect BEFORE the old connection's teardown runs.
        host2, created2, epoch2 = await reg.get_or_create(
            "RCN-0001", sid, "task-01-training-loop", cfg
        )
        assert host2 is host and created2 is False and epoch2 != epoch1

        # Old connection closes late, carrying its (now stale) epoch.
        await reg.connection_closed("RCN-0001", epoch1)
        await asyncio.sleep(0.05)

        assert reg.get("RCN-0001") is host, "reconnected session must survive"
        assert "RCN-0001" not in reg._grace_tasks
    finally:
        host_module.DISCONNECT_GRACE_SECONDS = orig


async def test_last_connection_close_schedules_grace():
    """The non-overlapping path still arms the grace timer."""
    import vibony_backend.agent_host.host as host_module
    from vibony_backend.agent_host.host import AgentHostRegistry

    orig = host_module.DISCONNECT_GRACE_SECONDS
    host_module.DISCONNECT_GRACE_SECONDS = 10  # long enough to inspect, not fire
    try:
        reg = AgentHostRegistry()
        host, _, epoch = await reg.get_or_create(
            "RCN-0002", uuid.uuid4(), "task-01-training-loop", {"primary_model": "m"}
        )
        await reg.connection_closed("RCN-0002", epoch)
        assert "RCN-0002" in reg._grace_tasks
        assert not reg._grace_tasks["RCN-0002"].done()
        reg._cancel_grace("RCN-0002")
    finally:
        host_module.DISCONNECT_GRACE_SECONDS = orig


async def test_stale_close_after_force_terminate_no_leak():
    """connection_closed after force_terminate must be a no-op (no 1 h grace leak)."""
    from vibony_backend.agent_host.host import AgentHostRegistry

    reg = AgentHostRegistry()
    host, _, epoch = await reg.get_or_create(
        "RCN-0003", uuid.uuid4(), "task-01-training-loop", {"primary_model": "m"}
    )
    await reg.force_terminate("RCN-0003")  # pops + terminates the host
    await reg.connection_closed("RCN-0003", epoch)

    assert reg.get("RCN-0003") is None
    assert "RCN-0003" not in reg._grace_tasks


async def test_duplicate_connection_close_does_not_grace_kill_live_peer():
    """A duplicate connection (same token — rejected at the
    presence join, but it still ran get_or_create and bumped the epoch) that
    disconnects first must NOT arm the grace timer while the first, live
    connection is still open. Arming is gated on the *last* connection closing."""
    import vibony_backend.agent_host.host as host_module
    from vibony_backend.agent_host.host import AgentHostRegistry

    orig = host_module.DISCONNECT_GRACE_SECONDS
    host_module.DISCONNECT_GRACE_SECONDS = 0  # would fire immediately if armed
    try:
        reg = AgentHostRegistry()
        sid = uuid.uuid4()
        cfg = {"primary_model": "m"}
        host, created, _epoch1 = await reg.get_or_create(
            "DUP-0001", sid, "task-01-training-loop", cfg
        )
        assert created is True
        # Duplicate connection: same host, bumps epoch, count → 2.
        host2, created2, epoch2 = await reg.get_or_create(
            "DUP-0001", sid, "task-01-training-loop", cfg
        )
        assert host2 is host and created2 is False
        # The duplicate disconnects first, carrying the now-current epoch.
        await reg.connection_closed("DUP-0001", epoch2)
        await asyncio.sleep(0.05)
        assert reg.get("DUP-0001") is host, "live first connection must survive"
        assert "DUP-0001" not in reg._grace_tasks
    finally:
        host_module.DISCONNECT_GRACE_SECONDS = orig


async def test_grace_arms_when_last_of_two_connections_closes():
    """The first connection's own close — the *last* live one — must still arm the
    grace timer even though its epoch is now stale (the duplicate bumped it). The
    old epoch-equality guard would wrongly skip it → a host that never grace-dies."""
    import vibony_backend.agent_host.host as host_module
    from vibony_backend.agent_host.host import AgentHostRegistry

    orig = host_module.DISCONNECT_GRACE_SECONDS
    host_module.DISCONNECT_GRACE_SECONDS = 10  # long enough to inspect, not fire
    try:
        reg = AgentHostRegistry()
        sid = uuid.uuid4()
        cfg = {"primary_model": "m"}
        host, _, epoch1 = await reg.get_or_create(
            "DUP-0002", sid, "task-01-training-loop", cfg
        )
        _, _, epoch2 = await reg.get_or_create(
            "DUP-0002", sid, "task-01-training-loop", cfg
        )
        await reg.connection_closed("DUP-0002", epoch2)  # dup closes → no grace yet
        assert "DUP-0002" not in reg._grace_tasks
        await reg.connection_closed("DUP-0002", epoch1)  # last (stale-epoch) close
        assert "DUP-0002" in reg._grace_tasks, "last connection must arm grace"
        assert not reg._grace_tasks["DUP-0002"].done()
        reg._cancel_grace("DUP-0002")
    finally:
        host_module.DISCONNECT_GRACE_SECONDS = orig


async def test_stale_host_teardown_preserves_fresh_proxy():
    """An old host's teardown must not unregister a fresh host's
    WebFsProxy (the "sandbox not ready" race after grace-then-reconnect)."""
    from vibony_backend.sessions.fs_proxy import web_fs_proxy_registry

    code = "FSPX-0001"
    host_old = _make_host(session_code=code)
    host_new = _make_host(session_code=code)
    with _start_patches():
        await host_old.start()
        proxy_old = web_fs_proxy_registry.get(code)
        await host_new.start()  # fresh host re-registers under the same code
        proxy_new = web_fs_proxy_registry.get(code)
        assert proxy_new is not None and proxy_new is not proxy_old

        await host_old.terminate()  # stale teardown
        assert web_fs_proxy_registry.get(code) is proxy_new, (
            "fresh host's proxy must survive the stale teardown"
        )
        await host_new.terminate()
    web_fs_proxy_registry.unregister(code)


# ---------------------------------------------------------------------------
# quiesce drains the in-flight turn
# ---------------------------------------------------------------------------


async def test_quiesce_drains_in_flight_turn_then_blocks_new():
    host = _make_host()
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def fake_chat_turn(**_kwargs):
        started.set()
        await release.wait()
        finished.set()

    with (
        patch("vibony_backend.agent_host.host.store_event", side_effect=_fake_store),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
        patch("vibony_backend.agent_host.host.chat_turn", side_effect=fake_chat_turn),
    ):
        await host.start()
        assert host.start_user_message("hi") is True
        await asyncio.wait_for(started.wait(), timeout=1.0)

        q = asyncio.create_task(host.quiesce())
        await asyncio.sleep(0.02)
        assert not q.done(), "quiesce must wait for the in-flight turn"

        release.set()
        await asyncio.wait_for(q, timeout=1.0)
        assert finished.is_set(), "turn finished cleanly before quiesce returned"

        # New turns are refused after quiesce (terminal transition in progress).
        assert host.start_user_message("again") is False
        assert host.start_inline_bash("echo x") is False


async def test_quiesce_cancels_overrunning_turn():
    host = _make_host()
    started = asyncio.Event()
    never = asyncio.Event()

    async def hanging_turn(**_kwargs):
        started.set()
        await never.wait()  # never completes

    with (
        patch("vibony_backend.agent_host.host.store_event", side_effect=_fake_store),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
        patch("vibony_backend.agent_host.host.chat_turn", side_effect=hanging_turn),
    ):
        await host.start()
        assert host.start_user_message("hi") is True
        await asyncio.wait_for(started.wait(), timeout=1.0)

        # A tiny timeout forces the cancel-fallback path.
        await asyncio.wait_for(host.quiesce(timeout=0.05), timeout=1.0)
        assert host._turn_task.done()


async def test_quiesce_propagates_own_cancellation():
    """If quiesce's own coroutine is cancelled (e.g. async_timer.cancel races the
    drain on the timeout path), it must re-raise — not swallow the cancellation."""
    host = _make_host()
    started = asyncio.Event()
    never = asyncio.Event()

    async def hanging_turn(**_kwargs):
        started.set()
        await never.wait()

    with (
        patch("vibony_backend.agent_host.host.store_event", side_effect=_fake_store),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
        patch("vibony_backend.agent_host.host.chat_turn", side_effect=hanging_turn),
    ):
        await host.start()
        assert host.start_user_message("hi") is True
        await asyncio.wait_for(started.wait(), timeout=1.0)

        q = asyncio.create_task(host.quiesce(timeout=100.0))  # blocks draining the turn
        await asyncio.sleep(0.02)
        assert not q.done()

        q.cancel()  # cancel quiesce itself
        with pytest.raises(asyncio.CancelledError):
            await q


# ---------------------------------------------------------------------------
# inline-bash serialised with the agent turn
# ---------------------------------------------------------------------------


async def test_run_bash_waits_for_turn_lock():
    host = _make_host()
    emitted: list[str] = []

    async def rec_emit(event_type, payload):
        emitted.append(event_type)

    with _start_patches():
        await host.start()
    host._sink.emit = rec_emit  # type: ignore[assignment]

    await host._turn_lock.acquire()  # simulate an agent turn holding the lock
    try:
        task = asyncio.create_task(host._run_bash("ls", "cid-1", "auto_allow"))
        await asyncio.sleep(0.05)
        assert "command_run" not in emitted, (
            "inline bash must not start while a turn holds _turn_lock"
        )
    finally:
        host._turn_lock.release()

    await asyncio.wait_for(task, timeout=2.0)
    assert "command_run" in emitted


# ---------------------------------------------------------------------------
# reset_sandbox vs terminate share _turn_lock
# ---------------------------------------------------------------------------


async def test_terminate_waits_for_turn_lock():
    host = _make_host()
    with _start_patches():
        await host.start()

    await host._turn_lock.acquire()  # simulate reset/turn holding the lock
    try:
        term = asyncio.create_task(host.terminate())
        await asyncio.sleep(0.05)
        assert not term.done(), "terminate must serialise teardown behind _turn_lock"
        assert host._terminated is True  # flag flips immediately
    finally:
        host._turn_lock.release()

    await asyncio.wait_for(term, timeout=1.0)
    assert host._terminated is True


async def test_reset_sandbox_bails_if_terminated_while_waiting_lock():
    host = _make_host()
    emitted: list[str] = []

    async def rec_emit(event_type, payload):
        emitted.append(event_type)

    with _start_patches():
        await host.start()
    host._sink.emit = rec_emit  # type: ignore[assignment]

    await host._turn_lock.acquire()  # block reset at the lock
    try:
        reset = asyncio.create_task(host.reset_sandbox())
        await asyncio.sleep(0.05)  # reset passed the top check, now blocked on the lock
        host._terminated = True  # a concurrent terminate flipped the flag
    finally:
        host._turn_lock.release()

    await asyncio.wait_for(reset, timeout=1.0)
    assert "sandbox_reset" not in emitted, (
        "reset must bail when the host was terminated while it waited for the lock"
    )


async def test_reset_sandbox_bails_if_terminated_during_rebuild():
    """Reset-vs-terminal-end: quiesce() does not drain reset_sandbox, so a
    terminal end can set _terminated *after* reset already holds the lock and
    finished teardown (terminate() flips the flag before blocking on _turn_lock).
    reset must re-check post-teardown and skip the re-provision + baseline/
    sandbox_reset emits — otherwise they land on the completed session as a burst
    of swallowed 'already completed' warnings and the rebuilt sandbox is wasted."""
    host = _make_host()
    emitted: list[str] = []

    async def rec_emit(event_type, payload):
        emitted.append(event_type)

    with _start_patches():
        await host.start()
    host._sink.emit = rec_emit  # type: ignore[assignment]
    emitted.clear()  # drop the start() baseline emit

    # Simulate a concurrent terminate() flipping _terminated *during* reset's
    # awaited teardown — i.e. after reset passed the top-of-function check.
    real_teardown = host._teardown_sandbox

    async def teardown_then_flip() -> None:
        await real_teardown()
        host._terminated = True

    host._teardown_sandbox = teardown_then_flip  # type: ignore[assignment]

    await asyncio.wait_for(host.reset_sandbox(), timeout=1.0)

    assert "sandbox_reset" not in emitted, "no sandbox_reset on a terminated host"
    assert "file_changed" not in emitted, "no re-seeded baseline on a terminated host"
    assert host._toolset is None, "reset must not rebuild a sandbox it then leaves"


# ---------------------------------------------------------------------------
# fire-and-forget tasks retained until done
# ---------------------------------------------------------------------------


async def test_spawn_retains_task_until_done():
    from vibony_backend.sessions import ws as ws_module

    started = asyncio.Event()
    release = asyncio.Event()

    async def work():
        started.set()
        await release.wait()

    task = ws_module._spawn(work())
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert task in ws_module._background_tasks, "task retained while running"

    release.set()
    await asyncio.wait_for(task, timeout=1.0)
    await asyncio.sleep(0)  # let the done-callback run
    assert task not in ws_module._background_tasks, "task discarded once done"

pytestmark = [pytest.mark.asyncio(loop_scope="session"), pytest.mark.usefixtures("isolated_agent_storage")]
