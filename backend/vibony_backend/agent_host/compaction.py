"""Conversation history compaction for the AgentHost.

Async interview sessions run 60+ minutes and the candidate can close/reopen the
tab. ``AgentHost._messages`` grows unbounded and every turn re-sends the whole
history, blowing token cost and latency. This module caps that growth by
*summarising* the old part of the conversation once a token budget is exceeded.

Design:

* The agent's **role and the task traps live in the system prompt** (injected by
  ``proxy.call_llm`` via ``render_weakness`` on every call), NOT in ``_messages``.
  Compaction only ever touches old back-and-forth turns — it can never make the
  agent forget its role or the traps.
* The blob ``_messages`` never contains the weakness system prompt. The running
  summary is stored as a ``role: "user"`` message: it is derived from
  candidate-authored turns, so it must never occupy a privileged ``system`` role
  where a planted "ignore your instructions" line would carry extra weight. It is
  recognised at the head of the blob by :data:`SUMMARY_PREFIX`, not by role. (Older
  sessions persisted a ``role: "system"`` summary; those are still recognised and
  get rewritten to ``role: "user"`` on the next fold.)
* The summariser call itself wraps the turns-to-fold in an untrusted-data fence
  so candidate text can't hijack the summariser either.
* Token estimation is a cheap ``len // 4`` heuristic — it only *gates* when to
  summarise, it is never used for correctness, so no tokeniser dependency.

Three pure-ish pieces:

* :func:`estimate_tokens` — heuristic size of a message list.
* :func:`select_for_summary` — pure split into (to_summarise, to_keep_verbatim)
  on turn boundaries; never splits a turn or orphans a ``role:tool`` message.
* :func:`summarise` — one cheap-model LLM call producing/extending the summary.

:func:`compact_messages` wires them together for the host.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import litellm
from vibony_backend.llm_usage import completion

from vibony_backend.prompt_safety import UNTRUSTED_DATA_GUIDANCE, wrap_untrusted
from vibony_backend.proxy import DEFAULT_MODEL

logger = logging.getLogger(__name__)

Message = dict[str, Any]

# Budget (in estimated tokens) for the live message blob. When exceeded after a
# turn, the oldest turns beyond the budget are folded into the summary.
HISTORY_TOKEN_BUDGET = int(os.environ.get("VIBONY_AGENT_HISTORY_TOKEN_BUDGET", "32000"))

# Cheap model for the summary call — deliberately not the agent's primary model
# (keep summary cost low). Falls back to the shared proxy default (DeepSeek)
# rather than re-reading VIBONY_DEFAULT_MODEL, so the default lives in one place
# (proxy.DEFAULT_MODEL) and cannot drift between the agent and this call site.
SUMMARY_MODEL = os.environ.get("VIBONY_AGENT_SUMMARY_MODEL") or DEFAULT_MODEL

# Marks our running summary so it is recognisable at the head of the blob (by
# content, not by role — stored as role:user) and so the prior summary can
# be stripped before folding new turns into it.
SUMMARY_PREFIX = "[Summary of earlier conversation]\n\n"

# Synthetic assistant turn inserted between the role:user summary and the first
# kept role:user turn so the compacted blob keeps strict user→assistant
# alternation. The summary is stored as role:user; without this
# bridge it sits directly before the first kept user turn → two consecutive
# role:user messages, which strict OpenAI-compatible chat endpoints reject (the
# litellm Anthropic adapter merges them, but we must not depend on the provider).
# It is our own fixed string — never candidate-derived — so it carries no
# injection risk in the (trusted) assistant role, and the next fold summarises it
# away, so at most one ever sits in the blob.
_SUMMARY_ACK = "Understood — I have the summary above and will continue from here."

# Per-message cap when rendering old turns into the summary prompt — keeps the
# summary call cheap even if a tool dumped a huge output.
_RENDER_CHAR_CAP = 2000

_SUMMARY_SYSTEM_PROMPT = (
    "You are a precise conversation summariser for an AI pair-programming "
    "session in a coding interview. Produce a faithful, compact summary of the "
    "earlier conversation so the assistant can continue with full context. "
    "Preserve: decisions made, files and code paths touched, the candidate's "
    "intent and current goal, unresolved questions, and any constraints. Do not "
    "invent facts, do not add commentary, do not address the user. Output only "
    "the summary prose.\n\n"
    # The conversation is candidate-authored — quarantine it so a
    # planted instruction can't hijack the summary.
    f"{UNTRUSTED_DATA_GUIDANCE}"
)


def estimate_tokens(messages: list[Message]) -> int:
    """Cheap ``len // 4`` token estimate for a message list.

    Counts string content plus a JSON dump of any ``tool_calls`` (their argument
    strings dominate tool-heavy turns). Gates compaction only — never used for
    correctness, so the imprecision is harmless.
    """
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += len(content)
        elif content is not None:
            total += len(json.dumps(content, ensure_ascii=False))
        tool_calls = m.get("tool_calls")
        if tool_calls:
            total += len(json.dumps(tool_calls, ensure_ascii=False))
    return total // 4


def _is_summary(message: Message) -> bool:
    """Recognise our running summary at the head of the blob.

    Identified by :data:`SUMMARY_PREFIX` content (which we control), not by role:
    stored as ``role: "user"`` so candidate-derived text never sits in a
    privileged ``system`` slot. Still matches a legacy ``role: "system"`` summary
    from an older session, which then gets rewritten to ``role: "user"`` on the
    next fold. The weakness system prompt is injected at call time, never here."""
    content = message.get("content")
    return isinstance(content, str) and content.startswith(SUMMARY_PREFIX)


def _group_turns(messages: list[Message]) -> list[list[Message]]:
    """Group a flat message list into turns without ever splitting one.

    * Our running summary (see :func:`_is_summary`) is its own single-message
      group wherever it appears (it lives at the head of the blob).
    * Each ``role: user`` message starts a new turn; the assistant replies and
      ``role: tool`` results that follow (until the next user) stay in that same
      group. This is the invariant that keeps a ``role:tool`` message from ever
      being orphaned from the ``assistant`` tool-call that produced it.
    """
    groups: list[list[Message]] = []
    current: list[Message] = []
    for m in messages:
        role = m.get("role")
        if _is_summary(m) or role == "system":
            # Our summary, or a stray system message — its own group either way.
            if current:
                groups.append(current)
                current = []
            groups.append([m])
        elif role == "user":
            if current:
                groups.append(current)
            current = [m]
        else:
            # assistant / tool — part of the current turn. A leading orphan
            # (no preceding user) defensively starts its own group.
            current.append(m)
    if current:
        groups.append(current)
    return groups


def select_for_summary(
    messages: list[Message], budget: int
) -> tuple[list[Message], list[Message]]:
    """Split ``messages`` into ``(to_summarise, to_keep_verbatim)`` on turn
    boundaries.

    Walks turns from the most recent backwards, keeping them verbatim while they
    fit ``budget``; everything older is returned for summarisation. The most
    recent turn is **always** kept even if it alone exceeds the budget (we cannot
    drop the turn the agent just produced). Pure function — no I/O.

    Never splits a turn and never orphans a ``role:tool`` message, because the
    split happens only between whole turn-groups (see :func:`_group_turns`).
    """
    groups = _group_turns(messages)
    if not groups:
        return [], []

    kept_rev: list[list[Message]] = []
    kept_tokens = 0
    split = 0  # number of leading groups that go to to_summarise
    for i in range(len(groups) - 1, -1, -1):
        g = groups[i]
        g_tokens = estimate_tokens(g)
        if kept_rev and kept_tokens + g_tokens > budget:
            split = i + 1
            break
        kept_rev.append(g)
        kept_tokens += g_tokens
    else:
        split = 0

    to_summarise = [m for g in groups[:split] for m in g]
    to_keep = [m for g in groups[split:] for m in g]
    return to_summarise, to_keep


def _truncate(text: str) -> str:
    if len(text) <= _RENDER_CHAR_CAP:
        return text
    return text[:_RENDER_CHAR_CAP] + "… [truncated]"


def _render_turns(messages: list[Message]) -> str:
    """Render old turns into plain text for the summary prompt."""
    lines: list[str] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        text = content if isinstance(content, str) else ""
        if role == "user":
            lines.append(f"User: {_truncate(text)}")
        elif role == "assistant":
            if text.strip():
                lines.append(f"Assistant: {_truncate(text)}")
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                name = fn.get("name", "?")
                args = fn.get("arguments", "")
                lines.append(f"Assistant called tool {name}({_truncate(str(args))})")
        elif role == "tool":
            lines.append(f"Tool result: {_truncate(text)}")
        elif role == "system":
            # A prior summary that slipped through — include it as context.
            lines.append(_truncate(text))
    return "\n".join(lines)


def _summary_message(text: str) -> Message:
    # role:user, not system: the summary is derived from candidate turns, so
    # it must not occupy a privileged role. Recognised by SUMMARY_PREFIX content.
    return {"role": "user", "content": SUMMARY_PREFIX + text.strip()}


def _ack_message() -> Message:
    # See _SUMMARY_ACK: bridges the role:user summary and the first kept role:user
    # turn so the blob alternates.
    return {"role": "assistant", "content": _SUMMARY_ACK}


async def summarise(
    old_turns: list[Message],
    prior_summary: str | None,
    model: str | None = None,
) -> Message:
    """One cheap-model LLM call folding ``old_turns`` into the summary.

    ``prior_summary`` (the body of the previous summary, prefix stripped) is
    extended incrementally rather than re-derived from scratch. Returns a single
    ``role: user`` summary message ready to sit at the head of the blob.
    """
    # The rendered turns are candidate-authored — fence them as untrusted
    # data so a planted instruction can't steer the summariser. The prior summary
    # is our own prior output; the system-prompt guidance covers it generically.
    convo = wrap_untrusted(_render_turns(old_turns), label="conversation turns")
    if prior_summary:
        user_content = (
            "Existing summary of the conversation so far:\n"
            f"{prior_summary}\n\n"
            "Newer conversation turns to fold in:\n"
            f"{convo}\n\n"
            "Return a single updated summary that integrates the new turns."
        )
    else:
        user_content = (
            "Conversation to summarise:\n"
            f"{convo}\n\n"
            "Return a single concise summary."
        )

    response = await completion("compaction",
        model=model or SUMMARY_MODEL,
        messages=[
            {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        timeout=120,
    )
    text = response.choices[0].message.content or ""
    return _summary_message(text)


async def compact_messages(
    messages: list[Message],
    budget: int = HISTORY_TOKEN_BUDGET,
    *,
    model: str | None = None,
) -> list[Message] | None:
    """Compact ``messages`` if it exceeds ``budget``; else return ``None``.

    Returns a new ``[summary, *recent_turns]`` list, or ``None`` when nothing
    can/should be done (under budget, or the only droppable thing is the existing
    summary with no new turns to fold in).
    """
    current = estimate_tokens(messages)
    if current <= budget:
        return None

    to_summarise, to_keep = select_for_summary(messages, budget)

    prior_summary: str | None = None
    old = to_summarise
    if old and _is_summary(old[0]):
        body = old[0].get("content") or ""
        prior_summary = body[len(SUMMARY_PREFIX):] if body.startswith(SUMMARY_PREFIX) else body
        old = old[1:]

    if not old:
        # Over budget, but the only thing droppable is the existing summary (or a
        # single live turn that alone exceeds the budget). We refuse to drop a
        # recent turn, so the blob stays over budget. Surface it: otherwise a
        # stuck session silently re-sends an oversized history on every turn with
        # no operator signal.
        logger.warning(
            "compact_messages: over budget (%d > %d) but nothing foldable beyond "
            "the recent turn(s); history left uncompacted",
            current,
            budget,
        )
        return None

    summary_msg = await summarise(old, prior_summary, model)

    # The summary is role:user, and a kept turn always starts with
    # a role:user message (see select_for_summary / _group_turns), so placing the
    # summary straight before to_keep would yield two consecutive role:user
    # messages. Bridge them with a synthetic assistant turn so the blob alternates.
    # When to_keep happens to start non-user (only reachable for a malformed blob;
    # select_for_summary never produces it — see the regression tests) the
    # summary already alternates against it, so the bridge is skipped.
    bridge = (
        [_ack_message()]
        if to_keep and to_keep[0].get("role") == "user"
        else []
    )
    result = [summary_msg, *bridge, *to_keep]

    # Guard against a pathological summary that is larger than the turns it
    # replaced (a cheap model can ramble). Compaction must never grow the blob —
    # if it did, keep the original and surface it. The estimate is the same cheap
    # heuristic used for the trigger, which is fine: we only need a rough
    # smaller/larger comparison, not an exact count.
    if estimate_tokens(result) >= current:
        logger.warning(
            "compact_messages: summary did not shrink history (%d -> %d); "
            "keeping original",
            current,
            estimate_tokens(result),
        )
        return None

    return result


__all__ = [
    "HISTORY_TOKEN_BUDGET",
    "SUMMARY_MODEL",
    "SUMMARY_PREFIX",
    "compact_messages",
    "estimate_tokens",
    "select_for_summary",
    "summarise",
]
