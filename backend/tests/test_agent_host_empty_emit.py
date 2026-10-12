"""The agent loop must not emit empty/degenerate bubbles.

Before this fix the terminal branch of ``chat_turn`` emitted an ``assistant_message``
unconditionally (``result.content or ""``), so a ``stop`` finish with empty/None
content produced an empty AI bubble; whitespace-only content slipped through the
tool-call branch's truthy guard too; and ``finish_reason`` was never inspected, so
``content_filter`` showed an empty bubble and ``length`` silently dropped a
truncated answer.

litellm is never hit — ``call_llm`` / ``dispatch_tool_call`` are patched.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

_session_loop = pytest.mark.asyncio(loop_scope="session")


class _FakeResult:
    """Minimal call_llm result mirroring the fields chat_turn reads."""

    def __init__(
        self,
        *,
        content: str | None = None,
        tool_calls: list[dict] | None = None,
        finish_reason: str | None = "stop",
        total_tokens: int = 0,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.finish_reason = finish_reason
        self.usage = {"total_tokens": total_tokens}


async def _run(results: list[_FakeResult]) -> list[str | None]:
    """Drive one chat_turn against a scripted sequence of LLM results.

    Returns the list of texts passed to emit_assistant_message.
    """
    from vibony_backend.agent_host.loop import chat_turn

    seq = iter(results)

    async def fake_call_llm(_req):
        return next(seq)

    async def fake_dispatch(_ts, _tc):
        return "tool ok"

    emitted: list[str | None] = []

    async def emit_assistant(text, _usage):
        emitted.append(text)

    async def emit_user(_text):
        pass

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
        )
    return emitted


# ---------------------------------------------------------------------------
# Empty & whitespace-only terminal content
# ---------------------------------------------------------------------------


@_session_loop
async def test_stop_with_empty_content_emits_nothing() -> None:
    emitted = await _run([_FakeResult(content="", finish_reason="stop")])
    assert emitted == []


@_session_loop
async def test_stop_with_none_content_emits_nothing() -> None:
    emitted = await _run([_FakeResult(content=None, finish_reason="stop")])
    assert emitted == []


@_session_loop
async def test_stop_with_whitespace_content_emits_nothing() -> None:
    emitted = await _run([_FakeResult(content="  \n\t ", finish_reason="stop")])
    assert emitted == []


@_session_loop
async def test_stop_with_real_content_emits_it() -> None:
    emitted = await _run([_FakeResult(content="all done", finish_reason="stop")])
    assert emitted == ["all done"]


@_session_loop
async def test_keeps_well_formed_blob_even_when_not_emitted() -> None:
    """The assistant turn is still appended to history (blob integrity), only the
    *visible* emit is suppressed."""
    from vibony_backend.agent_host.loop import chat_turn

    async def fake_call_llm(_req):
        return _FakeResult(content="", finish_reason="stop")

    emitted: list[str | None] = []

    async def emit_assistant(text, _usage):
        emitted.append(text)

    async def emit_user(_text):
        pass

    messages: list[dict] = []
    with patch("vibony_backend.agent_host.loop.call_llm", side_effect=fake_call_llm):
        await chat_turn(
            toolset=object(),
            task_id="t",
            messages=messages,
            user_text="hi",
            emit_user_message=emit_user,
            emit_assistant_message=emit_assistant,
        )

    assert emitted == []
    # user → assistant(""), so the next turn's history is still valid.
    assert messages[-1] == {"role": "assistant", "content": ""}


# ---------------------------------------------------------------------------
# Whitespace-only pre-tool-call content
# ---------------------------------------------------------------------------


@_session_loop
async def test_whitespace_pretool_content_not_emitted_but_tool_runs() -> None:
    """A turn whose first iteration has whitespace-only content + a tool call
    emits no pre-tool bubble; the real answer in the terminal iteration emits."""
    emitted = await _run(
        [
            _FakeResult(
                content="\n  ",
                tool_calls=[{"id": "tc1", "function": {"name": "Read", "arguments": "{}"}}],
                finish_reason="tool_calls",
            ),
            _FakeResult(content="here is the result", finish_reason="stop"),
        ]
    )
    assert emitted == ["here is the result"]


@_session_loop
async def test_pretool_content_emitted_when_present() -> None:
    emitted = await _run(
        [
            _FakeResult(
                content="let me check the file",
                tool_calls=[{"id": "tc1", "function": {"name": "Read", "arguments": "{}"}}],
                finish_reason="tool_calls",
            ),
            _FakeResult(content="", finish_reason="stop"),
        ]
    )
    # pre-tool text shows; the empty terminal close does not.
    assert emitted == ["let me check the file"]


# ---------------------------------------------------------------------------
# finish_reason inspection
# ---------------------------------------------------------------------------


@_session_loop
async def test_length_with_content_appends_truncated_notice() -> None:
    from vibony_backend.agent_host.loop import TRUNCATED_NOTICE

    emitted = await _run([_FakeResult(content="partial answer", finish_reason="length")])
    assert len(emitted) == 1
    assert emitted[0].startswith("partial answer")
    assert TRUNCATED_NOTICE in emitted[0]


@_session_loop
async def test_length_with_empty_content_emits_only_notice() -> None:
    from vibony_backend.agent_host.loop import TRUNCATED_NOTICE

    emitted = await _run([_FakeResult(content="", finish_reason="length")])
    assert emitted == [TRUNCATED_NOTICE]


@_session_loop
async def test_content_filter_empty_emits_notice_not_blank_bubble() -> None:
    from vibony_backend.agent_host.loop import CONTENT_FILTER_NOTICE

    emitted = await _run([_FakeResult(content=None, finish_reason="content_filter")])
    assert emitted == [CONTENT_FILTER_NOTICE]
