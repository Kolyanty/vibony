"""Controller reject-and-retry wiring inside chat_turn.

call_llm and the controller review are patched (no litellm, no DB). Verifies the
gate: a clean round passes through; a violating round is rejected and the assistant
regenerates; an unresolvable violation is let through after the retry cap; an
unlocked trap drops the violation; a fully-unlocked session skips the controller;
controller=None leaves the loop unchanged.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from vibony_backend.agent_host.controller import (
    MAX_CONTROLLER_RETRIES,
    ControllerVerdict,
    TrapViolation,
    build_hook,
)
from vibony_backend.agent_host.loop import chat_turn
from vibony_backend.assessment.inputs import Pitfall

_session_loop = pytest.mark.asyncio(loop_scope="session")


class _R:
    """Terminal call_llm result (no tool_calls → the turn ends after acceptance)."""

    def __init__(self, content: str = "", total_tokens: int = 0) -> None:
        self.content = content
        self.tool_calls = None
        self.usage = {"total_tokens": total_tokens}
        self.finish_reason = "stop"


def _hook(*, unlocked: set[int] | None = None):
    h = build_hook(
        [Pitfall(description="optimizer", flawed_behavior="no opt", correct_behavior="names opt")],
        "ctrl/model",
    )
    assert h is not None
    if unlocked:
        h.state.unlocked = set(unlocked)
    return h


def _violation():
    return ControllerVerdict(
        violations=[TrapViolation(trap=0, channel="chat", evidence="added optimizer", remove="that")]
    )


async def _run(call_results, review_verdicts, hook):
    emitted: list[str | None] = []

    async def emit_assistant(text, _usage):
        emitted.append(text)

    async def emit_user(_t):
        pass

    call_mock = AsyncMock(side_effect=list(call_results))
    review_mock = AsyncMock(side_effect=list(review_verdicts))
    with (
        patch("vibony_backend.agent_host.loop.call_llm", call_mock),
        patch("vibony_backend.agent_host.loop.review_turn", review_mock),
    ):
        await chat_turn(
            toolset=object(),
            task_id="t",
            messages=[],
            user_text="do the task",
            emit_user_message=emit_user,
            emit_assistant_message=emit_assistant,
            controller=hook,
        )
    return emitted, call_mock, review_mock


@_session_loop
async def test_clean_round_passes_through():
    emitted, call_mock, review_mock = await _run(
        [_R("resume implemented")],
        [ControllerVerdict()],  # no violation
        _hook(),
    )
    assert emitted == ["resume implemented"]
    assert call_mock.await_count == 1
    assert review_mock.await_count == 1


@_session_loop
async def test_violation_then_comply_regenerates():
    emitted, call_mock, review_mock = await _run(
        [_R("I'll add optimizer state"), _R("resume implemented")],
        [_violation(), ControllerVerdict()],  # reject, then clean
        _hook(),
    )
    # Only the corrected round is shown; the rejected attempt never reaches the candidate.
    assert emitted == ["resume implemented"]
    assert call_mock.await_count == 2  # original + 1 regeneration
    assert review_mock.await_count == 2

    # The regeneration's message history alternates (no consecutive role:user):
    # ...user(candidate), assistant(bridge=rejected text), user(correction).
    retry_req = call_mock.await_args_list[1].args[0]
    roles = [m["role"] for m in retry_req.messages]
    assert roles[-3:] == ["user", "assistant", "user"]
    assert "I'll add optimizer state" in retry_req.messages[-2]["content"]  # the bridge


@_session_loop
async def test_retry_skipped_once_budget_exceeded():
    from vibony_backend.agent_host.loop import SessionBudget

    # A 1-call budget: the first call_llm spends it, so the violating round is accepted
    # without burning a regeneration (the cap is respected inside the retry loop).
    budget = SessionBudget(max_llm_calls=1, max_tokens=0)
    emitted: list[str | None] = []

    async def emit_assistant(text, _usage):
        emitted.append(text)

    async def emit_user(_t):
        pass

    call_mock = AsyncMock(side_effect=[_R("added optimizer", total_tokens=1)])
    review_mock = AsyncMock(side_effect=[_violation()])
    with (
        patch("vibony_backend.agent_host.loop.call_llm", call_mock),
        patch("vibony_backend.agent_host.loop.review_turn", review_mock),
    ):
        await chat_turn(
            toolset=object(),
            task_id="t",
            messages=[],
            user_text="do it",
            emit_user_message=emit_user,
            emit_assistant_message=emit_assistant,
            controller=_hook(),
            budget=budget,
        )
    assert call_mock.await_count == 1  # no regeneration past the spent budget
    assert emitted == ["added optimizer"]


@_session_loop
async def test_unresolved_violation_let_through_after_cap():
    # The model never complies → after MAX_CONTROLLER_RETRIES the round is let through.
    n = 1 + MAX_CONTROLLER_RETRIES
    emitted, call_mock, review_mock = await _run(
        [_R(f"attempt {i}") for i in range(n)],
        [_violation() for _ in range(n)],
        _hook(),
    )
    assert call_mock.await_count == n
    assert review_mock.await_count == n
    assert emitted == [f"attempt {n - 1}"]  # the last (let-through) attempt


@_session_loop
async def test_max_retries_env_override(monkeypatch):
    # VIBONY_CONTROLLER_MAX_RETRIES=0 → no corrective regenerations; a violating round is
    # let through after the first review (the residual-3 lever, read per-turn).
    monkeypatch.setenv("VIBONY_CONTROLLER_MAX_RETRIES", "0")
    emitted, call_mock, review_mock = await _run(
        [_R("attempt 0"), _R("attempt 1")],  # a 2nd is available but must NOT be used
        [_violation(), _violation()],
        _hook(),
    )
    assert call_mock.await_count == 1  # no regeneration past a 0 cap
    assert review_mock.await_count == 1
    assert emitted == ["attempt 0"]


@_session_loop
async def test_unlock_this_turn_drops_violation():
    # The candidate named the trap this turn → newly_unlocked → the violation is not a
    # violation anymore; the round is accepted without a retry.
    verdict = ControllerVerdict(
        newly_unlocked=[0],
        violations=[TrapViolation(trap=0, channel="code", evidence="x", remove="y")],
    )
    hook = _hook()
    emitted, call_mock, review_mock = await _run([_R("optimizer added")], [verdict], hook)
    assert emitted == ["optimizer added"]
    assert call_mock.await_count == 1
    assert review_mock.await_count == 1
    assert hook.state.unlocked == {0}


@_session_loop
async def test_fully_unlocked_skips_controller():
    review_mock = AsyncMock()
    hook = _hook(unlocked={0})  # all policeable traps already named
    emitted, call_mock, review_mock = await _run([_R("anything")], [], hook)
    assert emitted == ["anything"]
    assert review_mock.await_count == 0  # controller off — no review call


@_session_loop
async def test_controller_none_leaves_loop_unchanged():
    review_mock = AsyncMock()
    call_mock = AsyncMock(side_effect=[_R("hello")])
    emitted: list[str | None] = []

    async def emit_assistant(text, _usage):
        emitted.append(text)

    async def emit_user(_t):
        pass

    with (
        patch("vibony_backend.agent_host.loop.call_llm", call_mock),
        patch("vibony_backend.agent_host.loop.review_turn", review_mock),
    ):
        await chat_turn(
            toolset=object(),
            task_id="t",
            messages=[],
            user_text="hi",
            emit_user_message=emit_user,
            emit_assistant_message=emit_assistant,
            controller=None,
        )
    assert emitted == ["hello"]
    review_mock.assert_not_called()


@_session_loop
@pytest.mark.parametrize("unlocked", [False, True])
async def test_agent_bash_waits_for_controller_unlock(unlocked):
    proposal = _R()
    proposal.tool_calls = [{"id": "shell-1", "function": {"name": "Bash", "arguments": '{"command":"echo unsafe > task.py"}'}}]
    messages = []
    hook = _hook(unlocked={0} if unlocked else None)
    with patch("vibony_backend.agent_host.loop.call_llm", AsyncMock(side_effect=[proposal, _R("done")])):
        with patch("vibony_backend.agent_host.loop.review_turn", AsyncMock(return_value=ControllerVerdict())):
            with patch("vibony_backend.agent_host.loop.dispatch_tool_call", AsyncMock(return_value="ok")) as dispatch:
                await chat_turn(toolset=object(), task_id="t", messages=messages,
                    user_text="help", emit_user_message=AsyncMock(), emit_assistant_message=AsyncMock(), controller=hook)
    assert dispatch.await_count == int(unlocked)
    result = next(m for m in messages if m["role"] == "tool")
    assert result["content"] == "ok" if unlocked else "unavailable" in result["content"]


@_session_loop
@pytest.mark.parametrize("stage", ["review", "retry", "after_tools"])
async def test_durable_quota_stops_at_safe_round_boundary(stage):
    from vibony_backend.llm_usage import UsageLimitExceeded
    from vibony_backend.agent_host.loop import BUDGET_EXHAUSTED_MESSAGE
    messages = []
    emit = AsyncMock()
    proposal = _R("private proposal")
    proposal.tool_calls = [{"id": "t1", "function": {"name": "Write", "arguments": '{"path":"a.py","content":"x"}'}}]
    calls = [proposal, UsageLimitExceeded()]
    reviews = [UsageLimitExceeded()] if stage == "review" else [_violation()]
    with patch("vibony_backend.agent_host.loop.call_llm", AsyncMock(side_effect=calls)), patch(
        "vibony_backend.agent_host.loop.review_turn", AsyncMock(side_effect=reviews)
    ), patch("vibony_backend.agent_host.loop.dispatch_tool_call", AsyncMock(return_value="ok")) as dispatch:
        await chat_turn(toolset=object(), task_id="t", messages=messages, user_text="continue",
            emit_user_message=AsyncMock(), emit_assistant_message=emit,
            controller=None if stage == "after_tools" else _hook())
    assert messages[-1] == {"role": "assistant", "content": BUDGET_EXHAUSTED_MESSAGE}
    if stage == "after_tools":
        dispatch.assert_awaited_once()
        assert messages[-2] == {"role": "tool", "tool_call_id": "t1", "content": "ok"}
    else:
        dispatch.assert_not_awaited()
        assert [m["role"] for m in messages] == ["user", "assistant"]
        emit.assert_awaited_once_with(BUDGET_EXHAUSTED_MESSAGE, None)


@_session_loop
async def test_quota_error_inside_tool_dispatch_still_propagates():
    from vibony_backend.llm_usage import UsageLimitExceeded
    proposal = _R()
    proposal.tool_calls = [{"id": "t1", "function": {"name": "Write", "arguments": '{}'}}]
    with patch("vibony_backend.agent_host.loop.call_llm", AsyncMock(return_value=proposal)), patch(
        "vibony_backend.agent_host.loop.dispatch_tool_call", AsyncMock(side_effect=UsageLimitExceeded())
    ):
        with pytest.raises(UsageLimitExceeded):
            await chat_turn(toolset=object(), task_id="t", messages=[], user_text="continue",
                emit_user_message=AsyncMock(), emit_assistant_message=AsyncMock())
