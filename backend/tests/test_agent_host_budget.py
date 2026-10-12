"""Per-session LLM cost cap.

SessionBudget caps cumulative LLM calls / tokens; chat_turn stops a runaway tool
loop and refuses a new turn once the budget is spent, surfacing a soft-stop
assistant_message (no new event type — no protocol bump). litellm is never hit:
call_llm / dispatch_tool_call are patched.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

# This file mixes sync SessionBudget unit tests with async chat_turn tests, so it
# uses a per-test asyncio decorator rather than a module-level pytestmark (the
# latter would spam asyncio warnings on the sync tests). loop_scope="session" is
# the same scope every other async/db test uses — see backend/CLAUDE.md "Test
# suite traps" for why a function-scoped async test wedges the suite.
_session_loop = pytest.mark.asyncio(loop_scope="session")


class _FakeResult:
    """Minimal call_llm result with a forced tool call so the loop keeps going."""

    def __init__(self, *, with_tool_call: bool, total_tokens: int = 0) -> None:
        self.content = ""
        self.tool_calls = (
            [{"id": "tc1", "function": {"name": "Read", "arguments": "{}"}}]
            if with_tool_call
            else None
        )
        self.usage = {"total_tokens": total_tokens}


# ---------------------------------------------------------------------------
# SessionBudget unit behaviour
# ---------------------------------------------------------------------------


def test_budget_records_calls_and_tokens() -> None:
    from vibony_backend.agent_host.loop import SessionBudget

    b = SessionBudget(max_llm_calls=5, max_tokens=100)
    b.record({"total_tokens": 30})
    b.record(None)  # provider omitted usage → only the call count advances
    assert b.llm_calls == 2
    assert b.total_tokens == 30
    assert b.exceeded() is False


def test_budget_exceeded_on_calls() -> None:
    from vibony_backend.agent_host.loop import SessionBudget

    b = SessionBudget(max_llm_calls=2, max_tokens=0)
    b.record(None)
    assert b.exceeded() is False
    b.record(None)
    assert b.exceeded() is True


def test_budget_exceeded_on_tokens() -> None:
    from vibony_backend.agent_host.loop import SessionBudget

    b = SessionBudget(max_llm_calls=0, max_tokens=50)
    b.record({"total_tokens": 49})
    assert b.exceeded() is False
    b.record({"total_tokens": 1})
    assert b.exceeded() is True


def test_budget_zero_limits_never_exceed() -> None:
    from vibony_backend.agent_host.loop import SessionBudget

    b = SessionBudget(max_llm_calls=0, max_tokens=0)
    for _ in range(100):
        b.record({"total_tokens": 10_000})
    assert b.exceeded() is False


# ---------------------------------------------------------------------------
# chat_turn enforcement
# ---------------------------------------------------------------------------


@_session_loop
async def test_chat_turn_stops_runaway_tool_loop_at_call_cap() -> None:
    from vibony_backend.agent_host.loop import (
        BUDGET_EXHAUSTED_MESSAGE,
        SessionBudget,
        chat_turn,
    )

    calls = {"n": 0}

    async def fake_call_llm(_req):
        calls["n"] += 1
        return _FakeResult(with_tool_call=True, total_tokens=10)

    async def fake_dispatch(_ts, _tc):
        return "tool ok"

    emitted: list[str | None] = []

    async def emit_assistant(text, _usage):
        emitted.append(text)

    async def emit_user(_text):
        pass

    budget = SessionBudget(max_llm_calls=3, max_tokens=0)

    with (
        patch("vibony_backend.agent_host.loop.call_llm", side_effect=fake_call_llm),
        patch("vibony_backend.agent_host.loop.dispatch_tool_call", side_effect=fake_dispatch),
    ):
        await chat_turn(
            toolset=object(),
            task_id="t",
            messages=[],
            user_text="hi",
            emit_user_message=emit_user,
            emit_assistant_message=emit_assistant,
            budget=budget,
        )

    # Exactly 3 LLM calls, then the top-of-loop budget check soft-stops.
    assert calls["n"] == 3
    assert BUDGET_EXHAUSTED_MESSAGE in emitted


@_session_loop
async def test_chat_turn_refuses_new_turn_when_already_exhausted() -> None:
    from vibony_backend.agent_host.loop import (
        BUDGET_EXHAUSTED_MESSAGE,
        SessionBudget,
        chat_turn,
    )

    calls = {"n": 0}

    async def fake_call_llm(_req):
        calls["n"] += 1
        return _FakeResult(with_tool_call=False)

    emitted: list[str | None] = []

    async def emit_assistant(text, _usage):
        emitted.append(text)

    async def emit_user(_text):
        pass

    budget = SessionBudget(max_llm_calls=2, max_tokens=0)
    budget.llm_calls = 5  # already over the cap from prior turns

    messages: list[dict] = []
    with patch("vibony_backend.agent_host.loop.call_llm", side_effect=fake_call_llm):
        await chat_turn(
            toolset=object(),
            task_id="t",
            messages=messages,
            user_text="please answer",
            emit_user_message=emit_user,
            emit_assistant_message=emit_assistant,
            budget=budget,
        )

    assert calls["n"] == 0  # never called the LLM
    assert emitted == [BUDGET_EXHAUSTED_MESSAGE]
    # History stays well-formed: user → assistant (the soft-stop).
    assert messages[-1] == {"role": "assistant", "content": BUDGET_EXHAUSTED_MESSAGE}


@_session_loop
async def test_chat_turn_bails_when_terminated() -> None:
    from vibony_backend.agent_host.loop import chat_turn

    calls = {"n": 0}

    async def fake_call_llm(_req):
        calls["n"] += 1
        return _FakeResult(with_tool_call=False)

    emitted: list[str | None] = []

    async def emit_assistant(text, _usage):
        emitted.append(text)

    async def emit_user(_text):
        pass

    with patch("vibony_backend.agent_host.loop.call_llm", side_effect=fake_call_llm):
        await chat_turn(
            toolset=object(),
            task_id="t",
            messages=[],
            user_text="hi",
            emit_user_message=emit_user,
            emit_assistant_message=emit_assistant,
            is_terminated=lambda: True,
        )

    assert calls["n"] == 0  # terminated → no LLM call
    assert emitted == []
