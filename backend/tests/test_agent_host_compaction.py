"""Tests for summarising conversation compaction.

Two layers:

1. Pure functions ``estimate_tokens`` / ``select_for_summary`` — turn-boundary
   splitting that never orphans a ``role:tool`` message and always keeps the most
   recent turn verbatim.
2. ``summarise`` / ``compact_messages`` — the LLM-backed fold, with
   ``litellm.acompletion`` mocked: old turns collapse into one ``role:user``
   summary at the head (never a privileged ``system`` role), the
   recent turns stay untouched, and the summariser input is fenced as untrusted.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from vibony_backend.agent_host.compaction import (
    SUMMARY_PREFIX,
    _SUMMARY_ACK,
    compact_messages,
    estimate_tokens,
    select_for_summary,
    summarise,
)


def _consecutive_same_role(messages: list[dict]) -> list[tuple[str, str]]:
    """Adjacent message pairs that share a role (the defect signature)."""
    roles = [m.get("role") for m in messages]
    return [(roles[i], roles[i + 1]) for i in range(len(roles) - 1) if roles[i] == roles[i + 1]]


def _orphaned_tools(messages: list[dict]) -> list[int]:
    """Indices of role:tool messages not immediately preceded by an assistant
    (the defect signature: a tool result severed from its tool_call)."""
    return [
        i
        for i, m in enumerate(messages)
        if m.get("role") == "tool"
        and (i == 0 or messages[i - 1].get("role") != "assistant")
    ]

# This file mixes sync pure-function tests with async ones, so it deliberately
# has no module-level pytestmark (that would mark the sync tests asyncio and spam
# warnings). The async tests below are decorated individually with
# loop_scope="session" — the SAME loop scope every other db/async test file in
# the suite uses. This is load-bearing: a function-scoped async test creates and
# tears down its own event loop, which leaves the session-scoped asyncpg
# connections (db_engine) bound to a now-dead loop and makes the NEXT db test
# fail with "got Future attached to a different loop". Keep these marks.
_session_loop = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _turn(size: int, marker: str = "x") -> list[dict]:
    """A plain user→assistant turn whose two messages each have ``size`` chars."""
    return [
        {"role": "user", "content": marker * size},
        {"role": "assistant", "content": marker * size},
    ]


def _tool_turn() -> list[dict]:
    """A turn with a full tool round: user → assistant(tool_calls) → tool → assistant."""
    return [
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command": "ls"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "train.py\n"},
        {"role": "assistant", "content": "done"},
    ]


def _fake_llm(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------


def test_estimate_tokens_counts_content_and_tool_calls() -> None:
    assert estimate_tokens([]) == 0
    # 400 chars of content → 100 tokens (len // 4).
    assert estimate_tokens([{"role": "user", "content": "a" * 400}]) == 100
    # tool_calls JSON contributes too.
    with_tools = estimate_tokens([_tool_turn()[1]])
    assert with_tools > 0


def test_estimate_tokens_handles_none_content() -> None:
    assert estimate_tokens([{"role": "assistant", "content": None}]) == 0


# ---------------------------------------------------------------------------
# select_for_summary
# ---------------------------------------------------------------------------


def test_select_under_budget_summarises_nothing() -> None:
    messages = _turn(40) + _turn(40)
    to_summarise, to_keep = select_for_summary(messages, budget=10_000)
    assert to_summarise == []
    assert to_keep == messages


def test_select_folds_oldest_keeps_recent() -> None:
    t0, t1, t2, t3 = _turn(400, "0"), _turn(400, "1"), _turn(400, "2"), _turn(400, "3")
    messages = t0 + t1 + t2 + t3  # each turn ≈ 200 tokens, total ≈ 800
    to_summarise, to_keep = select_for_summary(messages, budget=450)
    # Keeps t2 + t3 (400 ≤ 450), folds t0 + t1.
    assert to_summarise == t0 + t1
    assert to_keep == t2 + t3
    # The kept slice always starts a fresh turn.
    assert to_keep[0]["role"] == "user"


def test_select_never_orphans_a_tool_round() -> None:
    old = _turn(4000)  # ≈ 2000 tokens — must be folded
    tool_turn = _tool_turn()
    messages = old + tool_turn
    to_summarise, to_keep = select_for_summary(messages, budget=100)
    # The whole tool round stays together, intact, as the kept turn.
    assert to_keep == tool_turn
    assert to_keep[0]["role"] == "user"
    assert to_summarise == old
    # No kept message is an orphaned tool result.
    assert to_keep[0]["role"] not in ("tool", "assistant")


def test_select_always_keeps_the_most_recent_turn() -> None:
    old = _turn(40)
    huge = _turn(8000)  # ≈ 4000 tokens, far over budget
    messages = old + huge
    to_summarise, to_keep = select_for_summary(messages, budget=1)
    # The last turn is kept verbatim even though it alone busts the budget.
    assert to_keep == huge
    assert to_summarise == old


# ---------------------------------------------------------------------------
# summarise
# ---------------------------------------------------------------------------


@_session_loop
async def test_summarise_returns_user_message_with_prefix() -> None:
    old = _turn(40)
    with patch(
        "vibony_backend.agent_host.compaction.litellm.acompletion",
        new=AsyncMock(return_value=_fake_llm("the gist")),
    ) as mock_llm:
        msg = await summarise(old, prior_summary=None, model="model-x")

    # The stored summary is role:user, not a privileged system role.
    assert msg["role"] == "user"
    assert msg["content"].startswith(SUMMARY_PREFIX)
    assert "the gist" in msg["content"]
    # Cheap model threaded through, exactly one system + one user message sent.
    assert mock_llm.call_args.kwargs["model"] == "model-x"
    sent = mock_llm.call_args.kwargs["messages"]
    assert [m["role"] for m in sent] == ["system", "user"]


@_session_loop
async def test_summarise_fences_conversation_as_untrusted() -> None:
    """The candidate turns fed to the summariser are wrapped in an
    untrusted-data fence so a planted instruction can't hijack the summary."""
    with patch(
        "vibony_backend.agent_host.compaction.litellm.acompletion",
        new=AsyncMock(return_value=_fake_llm("ok")),
    ) as mock_llm:
        await summarise(_turn(40, "I"), prior_summary=None, model=None)

    sent = mock_llm.call_args.kwargs["messages"]
    sys_content = sent[0]["content"]
    user_content = sent[1]["content"]
    assert "UNTRUSTED DATA" in user_content  # the convo is fenced
    assert "<<<BEGIN UNTRUSTED DATA" in user_content
    assert "UNTRUSTED" in sys_content  # the guidance clause rides in the system prompt


@_session_loop
async def test_summarise_folds_prior_summary() -> None:
    with patch(
        "vibony_backend.agent_host.compaction.litellm.acompletion",
        new=AsyncMock(return_value=_fake_llm("updated")),
    ) as mock_llm:
        await summarise(_turn(40), prior_summary="EARLIER GIST", model=None)

    user_content = mock_llm.call_args.kwargs["messages"][1]["content"]
    assert "EARLIER GIST" in user_content


# ---------------------------------------------------------------------------
# compact_messages
# ---------------------------------------------------------------------------


@_session_loop
async def test_compact_under_budget_is_noop() -> None:
    messages = _turn(40) + _turn(40)
    assert await compact_messages(messages, budget=10_000) is None


@_session_loop
async def test_compact_folds_old_turns_into_head_summary() -> None:
    t0, t1, t2, t3 = _turn(400, "0"), _turn(400, "1"), _turn(400, "2"), _turn(400, "3")
    messages = t0 + t1 + t2 + t3

    with patch(
        "vibony_backend.agent_host.compaction.litellm.acompletion",
        new=AsyncMock(return_value=_fake_llm("folded")),
    ):
        result = await compact_messages(messages, budget=450)

    assert result is not None
    # Head is the summary (role:user); a synthetic assistant ack bridges it
    # to the kept turns (alternation), which then follow verbatim.
    assert result[0]["role"] == "user"
    assert result[0]["content"].startswith(SUMMARY_PREFIX)
    assert result[1] == {"role": "assistant", "content": _SUMMARY_ACK}
    assert result[2:] == t2 + t3
    # No two adjacent messages share a role.
    assert _consecutive_same_role(result) == []
    # Net smaller than the original blob.
    assert estimate_tokens(result) < estimate_tokens(messages)


@_session_loop
async def test_compact_does_not_orphan_tool_round() -> None:
    old = _turn(4000)
    tool_turn = _tool_turn()
    messages = old + tool_turn

    with patch(
        "vibony_backend.agent_host.compaction.litellm.acompletion",
        new=AsyncMock(return_value=_fake_llm("folded")),
    ):
        result = await compact_messages(messages, budget=100)

    assert result is not None
    # [summary, ack, *tool_turn] — the ack bridges the summary to the kept turn,
    # whose tool round stays intact.
    assert result[0]["role"] == "user"
    assert result[0]["content"].startswith(SUMMARY_PREFIX)
    assert result[1] == {"role": "assistant", "content": _SUMMARY_ACK}
    assert result[2:] == tool_turn
    assert _consecutive_same_role(result) == []
    assert _orphaned_tools(result) == []


@_session_loop
async def test_compact_incrementally_extends_existing_summary() -> None:
    # role:user prior summary is recognised by SUMMARY_PREFIX.
    prior = {"role": "user", "content": SUMMARY_PREFIX + "OLD SUMMARY"}
    t0, t1, t2 = _turn(400, "0"), _turn(400, "1"), _turn(400, "2")
    messages = [prior, *t0, *t1, *t2]

    with patch(
        "vibony_backend.agent_host.compaction.litellm.acompletion",
        new=AsyncMock(return_value=_fake_llm("merged")),
    ) as mock_llm:
        result = await compact_messages(messages, budget=450)

    assert result is not None
    # The prior summary body is fed back in for incremental folding (prefix stripped).
    user_content = mock_llm.call_args.kwargs["messages"][1]["content"]
    assert "OLD SUMMARY" in user_content
    assert SUMMARY_PREFIX not in user_content
    # Still exactly one summary message at the head.
    assert result[0]["role"] == "user"
    assert result[0]["content"].startswith(SUMMARY_PREFIX)
    assert sum(1 for m in result if m["content"].startswith(SUMMARY_PREFIX)) == 1


@_session_loop
async def test_compact_rewrites_legacy_system_summary_to_user() -> None:
    """Backward-compat: a legacy blob carries a role:system summary at the
    head. It is still recognised (by prefix), folded, and rewritten to role:user."""
    legacy = {"role": "system", "content": SUMMARY_PREFIX + "LEGACY GIST"}
    t0, t1, t2 = _turn(400, "0"), _turn(400, "1"), _turn(400, "2")
    messages = [legacy, *t0, *t1, *t2]

    with patch(
        "vibony_backend.agent_host.compaction.litellm.acompletion",
        new=AsyncMock(return_value=_fake_llm("merged")),
    ) as mock_llm:
        result = await compact_messages(messages, budget=450)

    assert result is not None
    user_content = mock_llm.call_args.kwargs["messages"][1]["content"]
    assert "LEGACY GIST" in user_content  # legacy body recognised + folded
    # The rewritten head no longer occupies the privileged system role.
    assert result[0]["role"] == "user"
    assert result[0]["content"].startswith(SUMMARY_PREFIX)
    assert not any(m["role"] == "system" for m in result)


@_session_loop
async def test_compact_noop_when_only_summary_is_droppable() -> None:
    prior = {"role": "user", "content": SUMMARY_PREFIX + "gist"}
    huge = _turn(8000)  # the one recent turn already busts the budget
    messages = [prior, *huge]
    # Nothing to fold beyond the existing summary → refuse to drop the recent turn.
    assert await compact_messages(messages, budget=1) is None


@_session_loop
async def test_compact_keeps_original_when_summary_does_not_shrink() -> None:
    t0, t1, t2 = _turn(400, "0"), _turn(400, "1"), _turn(400, "2")
    messages = t0 + t1 + t2
    # A pathological summary larger than the turns it replaced must not be
    # applied — compaction may never grow the blob.
    with patch(
        "vibony_backend.agent_host.compaction.litellm.acompletion",
        new=AsyncMock(return_value=_fake_llm("z" * 100_000)),
    ):
        result = await compact_messages(messages, budget=450)

    assert result is None


# ---------------------------------------------------------------------------
# History-shape: alternation + tool pairing
# ---------------------------------------------------------------------------


@_session_loop
async def test_compact_never_yields_consecutive_same_role() -> None:
    """The role:user summary must not sit directly before the first kept
    role:user turn — a synthetic assistant ack bridges them so the blob alternates."""
    t0, t1, t2, t3 = _turn(400, "0"), _turn(400, "1"), _turn(400, "2"), _turn(400, "3")
    messages = t0 + t1 + t2 + t3

    with patch(
        "vibony_backend.agent_host.compaction.litellm.acompletion",
        new=AsyncMock(return_value=_fake_llm("folded")),
    ):
        result = await compact_messages(messages, budget=450)

    assert result is not None
    assert _consecutive_same_role(result) == []
    # The bridge is exactly one assistant ack, right after the summary.
    assert result[0]["content"].startswith(SUMMARY_PREFIX)
    assert result[1] == {"role": "assistant", "content": _SUMMARY_ACK}


@_session_loop
async def test_compact_alternates_when_extending_prior_summary() -> None:
    """Alternation also holds on an incremental fold (prior summary already present)."""
    prior = {"role": "user", "content": SUMMARY_PREFIX + "OLD"}
    t0, t1, t2 = _turn(400, "0"), _turn(400, "1"), _turn(400, "2")
    messages = [prior, *t0, *t1, *t2]

    with patch(
        "vibony_backend.agent_host.compaction.litellm.acompletion",
        new=AsyncMock(return_value=_fake_llm("merged")),
    ):
        result = await compact_messages(messages, budget=450)

    assert result is not None
    assert _consecutive_same_role(result) == []
    # Exactly one ack ever sits in the blob (the prior one was folded into OLD).
    assert sum(1 for m in result if m.get("content") == _SUMMARY_ACK) == 1


@pytest.mark.parametrize(
    "blob_builder",
    [
        # Auto-welcome assistant message before the candidate's first turn.
        lambda: (
            [{"role": "assistant", "content": "Welcome! " + "w" * 400}]
            + _turn(400, "1")
            + _turn(400, "2")
            + _turn(400, "3")
        ),
        # Corrupt restored blob that begins on a bare tool result (no tool_call).
        lambda: (
            [{"role": "tool", "tool_call_id": "x", "content": "t" * 400}]
            + _turn(400, "1")
            + _turn(400, "2")
        ),
        # Tool-heavy tail turn that straddles the budget boundary.
        lambda: _turn(4000) + _tool_turn(),
    ],
    ids=["welcome-head", "bare-tool-head", "tool-tail-straddle"],
)
def test_select_keeps_clean_turn_boundary(blob_builder) -> None:
    """Refuted at the select layer: across adversarial blobs the kept
    window always starts on a role:user message and never orphans a role:tool from
    its tool_call — whole-turn grouping + head-orphan-folds-first already guard it."""
    messages = blob_builder()
    to_summarise, to_keep = select_for_summary(messages, budget=100)

    assert to_keep, "the most-recent turn is always kept"
    assert to_keep[0]["role"] == "user"
    # No kept role:tool is severed from its assistant tool_call.
    assert _orphaned_tools(to_keep) == []


@_session_loop
async def test_compact_tool_tail_keeps_tool_call_pairing() -> None:
    """A tool-heavy tail turn is kept whole, so its role:tool stays paired
    with the assistant tool_call that produced it — even past compaction."""
    blob = _turn(4000, "o") + _tool_turn()

    with patch(
        "vibony_backend.agent_host.compaction.litellm.acompletion",
        new=AsyncMock(return_value=_fake_llm("folded")),
    ):
        result = await compact_messages(blob, budget=100)

    assert result is not None
    assert _orphaned_tools(result) == []
    assert _consecutive_same_role(result) == []
    # The kept tool round survived intact at the tail.
    assert result[-2:] == _tool_turn()[-2:]
