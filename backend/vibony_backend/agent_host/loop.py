"""Agent loop for AgentHost.

Adapted from the legacy CLI agent loop. Key differences:
- LLM calls go directly to call_llm() (in-process), not through HTTP proxy.
- No REPL / chat_session — submit_user_message() in host.py replaces it.
- No inline-bash handling here.
- Duplication is intentional.

MAX_ITERATIONS=50 — guard against infinite tool-call loops.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable

from vibony_backend.proxy import call_llm
from vibony_backend.llm_usage import UsageLimitExceeded
from vibony_backend.schemas import ChatRequest

from .controller import (
    ControllerHook,
    build_correction,
    extract_proposed_edits,
    max_controller_retries,
    review_turn,
    round_is_reviewable,
)
from .tools import TOOL_DEFINITIONS, dispatch_tool_call

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 50

# Checkpointed assistant soft-stop counters. llm_usage.py additionally reserves
# all interview-role calls against the durable per-session quota before execution.
SESSION_MAX_LLM_CALLS = int(os.getenv("VIBONY_SESSION_MAX_LLM_CALLS", "1000"))
SESSION_MAX_TOKENS = int(os.getenv("VIBONY_SESSION_MAX_TOKENS", "10000000"))

# Soft-stop message emitted (as an assistant_message — no new event type, no
# protocol bump) when the budget is exhausted. Persists in the dialog so both the
# candidate and the interviewer see why the assistant went quiet.
BUDGET_EXHAUSTED_MESSAGE = (
    "The AI assistant has reached its usage limit for this session and can no "
    "longer respond. Please wrap up or let your interviewer know."
)

# Notices for non-`stop` terminal finish reasons. Without these a
# `content_filter` finish surfaces as an empty bubble and a `length` finish
# silently abandons a truncated answer. Both are low-frequency (no max_tokens set)
# but real and provider-dependent.
TRUNCATED_NOTICE = "(The response was cut off because it reached the length limit.)"
CONTENT_FILTER_NOTICE = "(The response was withheld by the model's content filter.)"


def _finish_reason_notice(finish_reason: str | None) -> str | None:
    """Map a non-`stop` terminal finish reason to a user-facing notice."""
    if finish_reason == "length":
        return TRUNCATED_NOTICE
    if finish_reason == "content_filter":
        return CONTENT_FILTER_NOTICE
    return None


class SessionBudget:
    """Cumulative per-session LLM-call / token budget.

    A dimension with a limit of 0 is unenforced. ``record`` is called once per
    ``call_llm`` round (the ``usage`` dict may be None when a provider omits it —
    then only the call count advances). ``exceeded`` is checked at the top of each
    ``chat_turn`` iteration, so it stops both a runaway tool loop *within* a turn
    and the start of a *new* turn once the budget is spent.
    """

    def __init__(self, max_llm_calls: int = 0, max_tokens: int = 0) -> None:
        self.max_llm_calls = max_llm_calls
        self.max_tokens = max_tokens
        self.llm_calls = 0
        self.total_tokens = 0

    def record(self, usage: dict[str, Any] | None) -> None:
        self.llm_calls += 1
        if usage:
            try:
                self.total_tokens += int(usage.get("total_tokens") or 0)
            except (TypeError, ValueError):
                pass

    def exceeded(self) -> bool:
        if self.max_llm_calls and self.llm_calls >= self.max_llm_calls:
            return True
        if self.max_tokens and self.total_tokens >= self.max_tokens:
            return True
        return False


async def chat_turn(
    toolset: Any,
    task_id: str,
    messages: list[dict[str, Any]],
    user_text: str,
    emit_user_message: Callable[[str], Awaitable[None]],
    emit_assistant_message: Callable[[str | None, dict | None], Awaitable[None]],
    model: str | None = None,
    session_code: str | None = None,
    ws_token: str | None = None,
    system_prompt: str | None = None,
    budget: SessionBudget | None = None,
    is_terminated: Callable[[], bool] | None = None,
    llm_timeout: Callable[[], float | None] | None = None,
    controller: ControllerHook | None = None,
    checkpoint: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """One turn: user_text → LLM response (with intermediate tool calls).

    Mutates `messages` (appends user, assistant, tool results).
    Emits user_message and assistant_message events through the callbacks.
    `session_code` / `ws_token` forwarded to ChatRequest for backend gate.
    `system_prompt`: pre-rendered weakness prompt from the DB task —
    forwarded so `call_llm` skips the filesystem resolve.
    `budget`: cumulative LLM-call/token cap; when exhausted the turn
    stops with a soft-stop assistant_message instead of calling the LLM again.
    `is_terminated`: polled before each LLM call so a terminated
    session stops its in-flight turn promptly (belt-and-suspenders to the task
    cancellation in AgentHost.terminate).
    `llm_timeout`: called before each LLM round to get a per-call timeout
    (seconds) or None — the AgentHost caps it at the remaining async budget so a
    hung call can't outlive the candidate's deadline.
    `controller`: the trap-control gate. When set, each reviewable assistant
    round is checked against the trap contracts *before* it is emitted/dispatched; a
    round that introduces/fixes/diagnoses a locked trap is rejected and the assistant
    regenerates under a grounded correction (reject-and-retry). None → the loop behaves
    exactly as before.
    """
    messages.append({"role": "user", "content": user_text})
    await emit_user_message(user_text)

    def _build_request(msgs: list[dict[str, Any]]) -> ChatRequest:
        return ChatRequest(
            task_id=task_id,
            messages=msgs,
            tools=TOOL_DEFINITIONS,
            model=model,
            session_code=session_code,
            system_prompt=system_prompt,
            timeout_seconds=llm_timeout() if llm_timeout is not None else None,
        )

    async def _accept_result(result: Any) -> Any:
        """Controller gate. Review the proposed round; on a violation, reject
        it and have the assistant regenerate under a grounded correction, up to
        :func:`max_controller_retries` (env-overridable). Returns
        the accepted (or retry-exhausted) result.

        Retries run on a LOCAL message copy (base ``messages`` + the correction) so the
        rejected attempt never lands in the persisted blob. Budget is charged for each
        regeneration. On exhaustion the round is let through and logged — the bench then
        measures the residual honestly."""
        if controller is None or controller.state.fully_unlocked():
            return result
        max_retries = max_controller_retries()  # read per-turn so a sweep's env takes effect
        retries = 0
        while True:
            edits = extract_proposed_edits(result.tool_calls)
            if not round_is_reviewable(edits, result.content):
                return result
            verdict = await review_turn(
                controller.contracts,
                controller.state.unlocked,
                user_text,
                result.content,
                edits,
                model=controller.model,
                state=controller.state,
            )
            controller.state.apply_unlock(verdict.newly_unlocked)
            if checkpoint:
                await checkpoint()
            violations = [
                v for v in verdict.violations if v.trap not in controller.state.unlocked
            ]
            if not violations:
                return result
            if (retries >= max_retries
                    or (is_terminated is not None and is_terminated())
                    or (budget is not None and budget.exceeded())):
                controller.state.unresolved_rounds += 1
                if checkpoint:
                    await checkpoint()
                logger.warning(
                    "controller: letting round through after %d retries — "
                    "unresolved violations on traps %s (session=%s)",
                    retries,
                    sorted(v.trap for v in violations),
                    session_code,
                )
                return result
            retries += 1
            # Reject-and-retry on a LOCAL copy. Bridge the base history (which ends with
            # the candidate's user_text) and the correction with a synthetic assistant
            # turn carrying the rejected attempt's text — this (a) keeps the strict
            # user→assistant→user alternation the rest of the codebase upholds (a bare
            # consecutive role:user is rejected by strict OpenAI-style
            # endpoints), and (b) shows the model exactly what it must undo. Only the
            # text is bridged (never the rejected tool_calls — that would dangle without
            # tool results); the correction already names the offending edits. Each retry
            # re-rolls from this base (the model doesn't accumulate prior corrections —
            # intentional independent re-roll, grounded on its latest attempt).
            bridge = (result.content or "").strip() or "(I proposed changes.)"
            correction = build_correction(violations, controller.contracts)
            retry_messages = messages + [
                {"role": "assistant", "content": bridge},
                {"role": "user", "content": correction},
            ]
            result = await call_llm(_build_request(retry_messages))
            if budget is not None:
                budget.record(result.usage)

    for _ in range(MAX_ITERATIONS):
        if is_terminated is not None and is_terminated():
            return
        if budget is not None and budget.exceeded():
            # Append the soft-stop to history so the blob stays well-formed
            # (user → assistant), then surface it once.
            messages.append({"role": "assistant", "content": BUDGET_EXHAUSTED_MESSAGE})
            await emit_assistant_message(BUDGET_EXHAUSTED_MESSAGE, None)
            return
        # Re-evaluated each iteration so the cap tracks the shrinking async budget
        # across a multi-round turn.
        try:
            result = await call_llm(_build_request(messages))
            if budget is not None:
                budget.record(result.usage)

            # No proposed output is emitted or dispatched until review succeeds.
            result = await _accept_result(result)
        except UsageLimitExceeded:
            # Reservations fail before a provider call. At this boundary all
            # previous tool results are complete, and the current proposal (if
            # any) is still private. Discard it and persist a normal soft stop.
            # Do not catch quota errors from tool dispatch below: those could
            # leave a partially executed tool round and must fail closed.
            messages.append({"role": "assistant", "content": BUDGET_EXHAUSTED_MESSAGE})
            await emit_assistant_message(BUDGET_EXHAUSTED_MESSAGE, None)
            return

        if result.tool_calls:
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": result.content or "",
                "tool_calls": result.tool_calls,
            }
            messages.append(assistant_msg)
            # A whitespace-only pre-tool message is still an empty bubble.
            if result.content and result.content.strip():
                await emit_assistant_message(result.content, result.usage)

            for tc in result.tool_calls:
                if (controller is not None and not controller.state.fully_unlocked()
                        and (tc.get("function") or {}).get("name") == "Bash"):
                    tool_output = ("Shell execution is unavailable in this session mode. "
                                   "Use Read/Glob/Grep for inspection and Write/Edit for changes.")
                else:
                    tool_output = await dispatch_tool_call(toolset, tc)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": tool_output,
                    }
                )
            continue

        # Terminal branch (no tool calls). Keep the blob well-formed (always append
        # the assistant turn) but only emit a *visible* bubble when there is
        # something to show — mirrors the tool-call branch's guard.
        messages.append({"role": "assistant", "content": result.content or ""})
        text = (result.content or "").strip()
        notice = _finish_reason_notice(result.finish_reason)
        if notice:
            # length → emit the partial answer + the notice; content_filter →
            # usually empty content, so just the notice (never a bare empty bubble).
            body = f"{result.content.rstrip()}\n\n{notice}" if text else notice
            await emit_assistant_message(body, result.usage)
        elif text:
            # A `stop` with empty/whitespace content emits nothing.
            await emit_assistant_message(result.content, result.usage)
        return

    # Hit MAX_ITERATIONS without a final answer — emit last content as assistant.
    await emit_assistant_message("(max iterations reached)", None)


__all__ = [
    "chat_turn",
    "MAX_ITERATIONS",
    "SessionBudget",
    "SESSION_MAX_LLM_CALLS",
    "SESSION_MAX_TOKENS",
    "BUDGET_EXHAUSTED_MESSAGE",
    "TRUNCATED_NOTICE",
    "CONTENT_FILTER_NOTICE",
]
