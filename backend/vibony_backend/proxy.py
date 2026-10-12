"""LLM AI proxy: injects the weakness system prompt and calls the model via litellm.

Stateless: every request carries the full message history. The proxy keeps no state
between requests; sessions and the event log live in the session layer.
"""

from __future__ import annotations

import logging
import os

import litellm
from vibony_backend.llm_usage import completion

from vibony_backend.schemas import ChatRequest, ChatResponse
from vibony_backend.weakness import render_weakness

logger = logging.getLogger(__name__)

# Disable litellm's anonymous usage telemetry in-code (don't rely on an env var being
# set at deploy time) — no outbound analytics from the LLM layer.
litellm.telemetry = False

DEFAULT_MODEL = os.environ.get("VIBONY_DEFAULT_MODEL", "deepseek/deepseek-chat")

# Absolute ceiling on a single LLM call (seconds). A hung or very slow provider call
# otherwise stalls the whole agent turn — and on a timed async session it eats the
# candidate's budget while they're locked out. The agent loop additionally caps this at
# the remaining async budget per call (ChatRequest.timeout_seconds).
LLM_CALL_TIMEOUT_SECONDS = float(os.environ.get("VIBONY_LLM_CALL_TIMEOUT_SECONDS", "300"))


class ModelNotAllowed(ValueError):
    """Raised when a requested model is rejected by the allowlist / SSRF guard."""


def _model_allowlist() -> set[str]:
    """Allowed model strings: always the server ``DEFAULT_MODEL``, plus any in
    ``VIBONY_MODEL_ALLOWLIST`` (comma-separated). Unset → only the default, so a
    user-supplied string can never pick an arbitrary provider/model.
    """
    allow = {DEFAULT_MODEL}
    raw = os.getenv("VIBONY_MODEL_ALLOWLIST", "").strip()
    if raw:
        allow |= {m.strip() for m in raw.split(",") if m.strip()}
    return allow


# A short, fixed, task-agnostic non-initiative reminder. In `end` mode it is appended
# after the candidate's latest message so the directive has recency at the moment the
# assistant decides whether to over-deliver. Constant (no candidate-derived text) → no
# injection surface. The full system prompt still leads the array (context + tools), so
# this is "restate the directive at the tail", NOT "move the whole prompt" — moving
# everything after role:tool results breaks the agent's tool loop (assistants abandon the
# task after reading files).
_NON_INITIATIVE_REMINDER = (
    "Reminder: follow the system instructions above exactly. Do ONLY what the candidate "
    "explicitly asked for in their latest message — do not proactively add, fix, improve, "
    "or point out anything they did not specifically request."
)


def _prompt_position() -> str:
    """Where to reinforce the weakness directive: ``start`` (default) or ``end``.

    The static non-initiative directive lives at ``messages[0]``, far from the point of
    action — by the time the model is several tool-rounds deep writing code, its salience
    has decayed. ``end`` keeps the full system prompt at the head (context/tools intact)
    and **additionally** appends :data:`_NON_INITIATIVE_REMINDER` after the candidate's
    latest message, testing whether a salient tail reminder holds the planted trap better.
    Default ``start`` keeps production behavior byte-identical. Read per call (not at
    import) so the value can be toggled in-process.
    """
    pos = os.getenv("VIBONY_PROMPT_POSITION", "start").strip().lower()
    return pos if pos in ("start", "end") else "start"


def validate_model(model: str) -> str:
    """Reject SSRF and non-allowlisted models.

    litellm interprets ``provider/model@https://host`` (and URL-bearing strings) as a
    custom endpoint, so an attacker-controlled model string is an SSRF primitive — the
    backend would POST to that URL. Reject any ``@``/scheme, then enforce the allowlist.
    Called from ``call_llm`` (covers every LLM turn) and at session-create (early,
    friendly error)."""
    if "@" in model or "://" in model:
        raise ModelNotAllowed(
            f"model must not embed an endpoint/api_base: {model!r}"
        )
    allow = _model_allowlist()
    if model not in allow:
        raise ModelNotAllowed(
            f"model {model!r} is not allowed (set VIBONY_MODEL_ALLOWLIST)"
        )
    return model


async def call_llm(request: ChatRequest) -> ChatResponse:
    """Calls the LLM via litellm with the weakness prompt injected.

    Messages are transformed as:
        [system: weakness] + request.messages

    If request.messages already contains a system message it is NOT overwritten; the
    backend injects its own system message first and the rest follow. This lets a caller
    add its own context (e.g. a task description) without losing the weakness instructions.

    If ``request.system_prompt`` is set (the web AgentHost renders it once from the DB
    task at session start) it is used verbatim; otherwise the legacy filesystem resolve
    ``render_weakness(task_id)`` is used (the /v1/chat path).

    With ``VIBONY_PROMPT_POSITION=end`` the full system prompt stays first (context/tools
    intact) and a short fixed non-initiative reminder is appended at the tail — but only
    when the last message is NOT ``role:tool`` (tool-safe: appending a system message
    after tool results breaks the tool loop). Default ``start`` leaves behavior unchanged.
    """
    system_prompt = request.system_prompt or render_weakness(request.task_id)
    messages = [{"role": "system", "content": system_prompt}, *request.messages]
    if (
        _prompt_position() == "end"
        and request.messages
        and request.messages[-1].get("role") != "tool"
    ):
        messages.append({"role": "system", "content": _NON_INITIATIVE_REMINDER})
    model = validate_model(request.model or DEFAULT_MODEL)

    logger.info(
        "chat request: task_id=%s model=%s messages_count=%d tools=%s",
        request.task_id,
        model,
        len(messages),
        bool(request.tools),
    )

    completion_kwargs: dict = {"model": model, "messages": messages}
    if request.tools:
        completion_kwargs["tools"] = request.tools

    # Per-call timeout: the agent loop may cap it at the remaining async budget; falls
    # back to the absolute ceiling for live / unbudgeted calls.
    timeout = request.timeout_seconds or LLM_CALL_TIMEOUT_SECONDS
    response = await completion("assistant", **completion_kwargs, timeout=timeout)

    choice = response.choices[0]
    message = choice.message
    tool_calls = None
    if getattr(message, "tool_calls", None):
        tool_calls = [tc.model_dump() if hasattr(tc, "model_dump") else dict(tc) for tc in message.tool_calls]

    usage = None
    if getattr(response, "usage", None):
        usage = response.usage.model_dump() if hasattr(response.usage, "model_dump") else dict(response.usage)

    logger.info(
        "chat response: finish_reason=%s tool_calls=%d usage=%s",
        choice.finish_reason,
        len(tool_calls) if tool_calls else 0,
        usage,
    )

    return ChatResponse(
        content=message.content,
        tool_calls=tool_calls,
        finish_reason=choice.finish_reason,
        usage=usage,
        model=model,
    )
