"""Durable per-session LLM reservations and usage, shared by all model roles.

Calls are reserved before contacting the provider, including failed/cancelled calls.
Token limits are soft: usage is known only after a reply; concurrent in-flight calls
can overshoot them. Assessment has a separate lifetime quota from the interview.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal

import anyio
import litellm
from sqlalchemy import select

from vibony_backend.db.engine import get_sessionmaker
from vibony_backend.db.models import Session
from vibony_backend.sessions.runtime_store import PersistenceError

LLMRole = Literal["assistant", "controller", "compaction", "assessment"]
_session: ContextVar[str | None] = ContextVar("llm_session", default=None)


class UsageLimitExceeded(RuntimeError):
    """A durable session quota prevents another provider request."""


@contextmanager
def session_usage(code: str | None) -> Iterator[None]:
    token = _session.set(code)
    try:
        yield
    finally:
        _session.reset(token)


def _limits(role: LLMRole) -> tuple[int, int]:
    assessment = role == "assessment"
    prefix = "VIBONY_ASSESSMENT" if assessment else "VIBONY_SESSION"
    return (
        int(os.getenv(f"{prefix}_MAX_LLM_CALLS", "20" if assessment else "1000")),
        int(os.getenv(f"{prefix}_MAX_TOKENS", "200000" if assessment else "10000000")),
    )


async def _update_row(
    code: str, role: LLMRole, model: str, usage: dict[str, Any] | None, *, reserve: bool
) -> None:
    async with get_sessionmaker()() as db, db.begin():
        result = await db.execute(
            select(Session).where(Session.code == code).with_for_update()
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise PersistenceError("Session no longer exists")
        ledger = dict(row.llm_usage or {})
        entry = dict(ledger.get(role, {}))
        if reserve:
            relevant = [
                value for key, value in ledger.items()
                if (key == "assessment") == (role == "assessment")
            ]
            calls, tokens = _limits(role)
            if (
                calls > 0 and sum(v.get("calls", 0) for v in relevant) >= calls
                or tokens > 0 and sum(v.get("tokens", 0) for v in relevant) >= tokens
            ):
                raise UsageLimitExceeded("Session LLM quota exhausted")
            entry["calls"] = entry.get("calls", 0) + 1
            entry["pending"] = entry.get("pending", 0) + 1
            entry["models"] = sorted(set(entry.get("models", []) + [model]))
        else:
            entry["pending"] = max(0, entry.get("pending", 0) - 1)
            if usage is None:
                entry["unknown_usage"] = entry.get("unknown_usage", 0) + 1
            else:
                total = usage.get("total_tokens")
                if total is None:
                    prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
                    output = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
                    total = prompt + output
                entry["tokens"] = entry.get("tokens", 0) + max(0, int(total))
        ledger[role] = entry
        row.llm_usage = ledger


async def _update(*args: Any, **kwargs: Any) -> None:
    try:
        await _update_row(*args, **kwargs)
    except (UsageLimitExceeded, PersistenceError):
        raise
    except Exception as exc:
        raise PersistenceError("Could not persist LLM usage") from exc


async def completion(role: LLMRole, **kwargs: Any) -> Any:
    """Keep the LiteLLM seam, adding reservations only within a session context."""
    code = _session.get()
    model = str(kwargs.get("model", ""))
    if code is not None:
        await _update(code, role, model, None, reserve=True)
    usage = None
    try:
        response = await litellm.acompletion(**kwargs)
        raw = getattr(response, "usage", None)
        if isinstance(raw, dict):
            usage = raw
        elif raw is not None and hasattr(raw, "model_dump"):
            value = raw.model_dump()
            if isinstance(value, dict):
                usage = value
        return response
    finally:
        if code is not None:
            # Account for cancellation before the host can be recreated.
            with anyio.CancelScope(shield=True):
                await _update(code, role, model, usage, reserve=False)
