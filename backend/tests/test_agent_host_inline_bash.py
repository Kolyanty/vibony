"""Tests for AgentHost.submit_inline_bash.

Mocks store_event and set_ws_send callback so no DB or WS required.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_host(**kwargs):
    from vibony_backend.agent_host.host import AgentHost

    return AgentHost(
        session_id=kwargs.get("session_id", uuid.uuid4()),
        session_code=kwargs.get("session_code", "TEST-8500"),
        task_id=kwargs.get("task_id", "task-01-training-loop"),
        ai_config=kwargs.get(
            "ai_config",
            {"primary_model": "test-model"},
        ),
    )


async def _capture(lst, t, p):
    lst.append((t, p))
    return len(lst)


# ---------------------------------------------------------------------------
# Test: allow path emits command_run / command_output / command_finished
# ---------------------------------------------------------------------------


async def test_allow_cmd_emits_run_output_finished():
    """classify_bash('echo hi') == allow → run immediately, no confirm."""
    emitted: list[tuple[str, dict]] = []
    host = _make_host()

    async def fake_store(sid, code, t, p, **kw):
        emitted.append((t, p))
        return len(emitted)

    with (
        patch("vibony_backend.agent_host.host.store_event", side_effect=fake_store),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
    ):
        await host.start()
        emitted.clear()

        await host.submit_inline_bash("echo hi")

    types = [t for t, _ in emitted]
    assert "command_run" in types
    assert "command_finished" in types
    run_payload = next(p for t, p in emitted if t == "command_run")
    assert run_payload["command"] == "echo hi"
    assert run_payload.get("permission_decision") == "auto_allow"
    fin_payload = next(p for t, p in emitted if t == "command_finished")
    assert fin_payload["exit_code"] == 0


# ---------------------------------------------------------------------------
# Test: local-mode stderr is labelled stream='stderr'
# ---------------------------------------------------------------------------


async def test_local_bash_labels_stderr_stream():
    """In local mode stderr chunks carry stream='stderr', not 'stdout'.

    `ls` of a missing path writes only to stderr; before the fix it was mislabeled
    as stdout. Local mode reads stdout/stderr as separate pipes (docker mode merges
    them into one stream and legitimately labels everything stdout).
    """
    emitted: list[tuple[str, dict]] = []
    host = _make_host()

    async def fake_store(sid, code, t, p, **kw):
        emitted.append((t, p))
        return len(emitted)

    with (
        patch("vibony_backend.agent_host.host.store_event", side_effect=fake_store),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
    ):
        await host.start()
        emitted.clear()

        await host.submit_inline_bash("ls /nonexistent_b_sess_9_path")

    outputs = [p for t, p in emitted if t == "command_output"]
    # The error text must surface under the stderr stream, not stdout.
    assert any(o["stream"] == "stderr" and o["data"] for o in outputs), outputs
    assert not any(o["stream"] == "stdout" and "nonexistent_b_sess_9_path" in o["data"] for o in outputs)


# ---------------------------------------------------------------------------
# Test: deny path (policy_deny) — no execution
# ---------------------------------------------------------------------------


async def test_deny_cmd_emits_policy_deny_no_output():
    """classify_bash('rm -rf /') == deny → policy_deny, no execution."""
    emitted: list[tuple[str, dict]] = []
    host = _make_host()

    async def fake_store(sid, code, t, p, **kw):
        emitted.append((t, p))
        return len(emitted)

    with (
        patch("vibony_backend.agent_host.host.store_event", side_effect=fake_store),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
    ):
        await host.start()
        emitted.clear()

        await host.submit_inline_bash("rm -rf /")

    types = [t for t, _ in emitted]
    assert "command_run" in types
    assert "command_output" not in types
    assert "command_finished" in types
    run_payload = next(p for t, p in emitted if t == "command_run")
    assert run_payload.get("permission_decision") == "policy_deny"
    fin_payload = next(p for t, p in emitted if t == "command_finished")
    assert fin_payload["exit_code"] == 1


# ---------------------------------------------------------------------------
# Test: confirm path — allow_once round-trip runs the command
# ---------------------------------------------------------------------------


async def test_confirm_allow_once_executes():
    """confirm path: _ws_send receives bash_confirm_request; allow_once runs cmd."""
    emitted: list[tuple[str, dict]] = []
    sent_frames: list[dict] = []
    host = _make_host()

    async def fake_store(sid, code, t, p, **kw):
        emitted.append((t, p))
        return len(emitted)

    async def fake_ws_send(frame):
        sent_frames.append(frame)
        if frame.get("kind") == "bash_confirm_request":
            cmd_id = frame["cmd_id"]
            async def _resolve():
                await asyncio.sleep(0)
                host.resolve_bash_confirm(cmd_id, "allow_once")
            asyncio.create_task(_resolve())

    with (
        patch("vibony_backend.agent_host.host.store_event", side_effect=fake_store),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
    ):
        await host.start()
        host.set_ws_send(fake_ws_send)
        emitted.clear()

        await host.submit_inline_bash("pip install numpy")

    confirm_frames = [m for m in sent_frames if m.get("kind") == "bash_confirm_request"]
    assert len(confirm_frames) == 1

    types = [t for t, _ in emitted]
    assert "command_run" in types
    assert "command_finished" in types
    run_payload = next(p for t, p in emitted if t == "command_run")
    assert run_payload.get("permission_decision") == "user_allow"


# ---------------------------------------------------------------------------
# Test: confirm path — deny choice → user_deny, no execution
# ---------------------------------------------------------------------------


async def test_confirm_deny_choice_emits_user_deny():
    """confirm path: resolving with 'deny' emits user_deny, no subprocess."""
    emitted: list[tuple[str, dict]] = []
    sent_frames: list[dict] = []
    host = _make_host()

    async def fake_store(sid, code, t, p, **kw):
        emitted.append((t, p))
        return len(emitted)

    async def fake_ws_send(frame):
        sent_frames.append(frame)
        if frame.get("kind") == "bash_confirm_request":
            cmd_id = frame["cmd_id"]
            async def _resolve():
                await asyncio.sleep(0)
                host.resolve_bash_confirm(cmd_id, "deny")
            asyncio.create_task(_resolve())

    with (
        patch("vibony_backend.agent_host.host.store_event", side_effect=fake_store),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
    ):
        await host.start()
        host.set_ws_send(fake_ws_send)
        emitted.clear()

        await host.submit_inline_bash("pip install numpy")

    types = [t for t, _ in emitted]
    assert "command_run" in types
    assert "command_output" not in types
    run_payload = next(p for t, p in emitted if t == "command_run")
    assert run_payload.get("permission_decision") == "user_deny"
    fin_payload = next(p for t, p in emitted if t == "command_finished")
    assert fin_payload["exit_code"] == 1


# ---------------------------------------------------------------------------
# Test: allow_prefix saves prefix and subsequent commands skip confirm
# ---------------------------------------------------------------------------


async def test_allow_prefix_saves_prefix_for_subsequent_commands():
    """allow_prefix: prefix saved; next 'pip install ...' cmd is auto_allow."""
    emitted: list[tuple[str, dict]] = []
    sent_frames: list[dict] = []
    host = _make_host()

    async def fake_store(sid, code, t, p, **kw):
        emitted.append((t, p))
        return len(emitted)

    call_count = {"n": 0}

    async def fake_ws_send(frame):
        sent_frames.append(frame)
        if frame.get("kind") == "bash_confirm_request":
            cmd_id = frame["cmd_id"]
            call_count["n"] += 1
            async def _resolve():
                await asyncio.sleep(0)
                host.resolve_bash_confirm(cmd_id, "allow_prefix")
            asyncio.create_task(_resolve())

    with (
        patch("vibony_backend.agent_host.host.store_event", side_effect=fake_store),
        patch("vibony_backend.agent_host.host._load_loaded_task", return_value=None),
    ):
        await host.start()
        host.set_ws_send(fake_ws_send)
        emitted.clear()

        # First command triggers confirm; user chooses allow_prefix.
        await host.submit_inline_bash("pip install numpy")
        emitted.clear()

        # Second pip install should be auto_allow (prefix already saved).
        await host.submit_inline_bash("pip install pandas")

    # No new confirm published for the second command
    second_confirms = [
        m for m in sent_frames[1:]  # skip first command's confirm
        if m.get("kind") == "bash_confirm_request"
    ]
    assert len(second_confirms) == 0

    run_payload = next(p for t, p in emitted if t == "command_run")
    assert run_payload.get("permission_decision") == "auto_allow"

pytestmark = [pytest.mark.asyncio(loop_scope="session"), pytest.mark.usefixtures("isolated_agent_storage")]
