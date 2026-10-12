"""Event invariant checks.

Matches docs/protocol/events.md, section "Invariants".
Called from the WS handler before writing an event to the DB (inside a
transaction with FOR UPDATE on the session, so the checks and the write are atomic).

Invariants checked here:
  1. `session_start` — the first event (no event exists before it).
  2. Any event other than `session_start` — session_start already exists.
  3. `tool_result.tool_call_id` — references an existing tool_call.
  4. `diff_accepted`/`diff_rejected.proposal_id` — references a diff_proposed.
  5. `command_output`/`command_finished.command_id` — references a command_run.

The "no events after session_end" invariant is checked earlier (via the session
status), so it is not duplicated here.
Invariant 6 (dedup by id) is handled in ws.py before this module is called.

Returns None if everything is fine, or a string describing the error.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vibony_backend.db.models import Event


async def check_invariants(
    db: AsyncSession,
    session_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
) -> str | None:
    if event_type == "session_start":
        return await _check_session_start(db, session_id)

    err = await _check_session_started(db, session_id, event_type)
    if err:
        return err

    if event_type == "tool_result":
        return await _check_ref(
            db, session_id, "tool_call", "tool_call_id", payload, event_type
        )

    if event_type in ("diff_accepted", "diff_rejected"):
        return await _check_ref(
            db, session_id, "diff_proposed", "proposal_id", payload, event_type
        )

    if event_type in ("command_output", "command_finished"):
        return await _check_ref(
            db, session_id, "command_run", "command_id", payload, event_type
        )

    return None


async def _check_session_start(db: AsyncSession, session_id: uuid.UUID) -> str | None:
    count = (
        await db.execute(
            select(func.count()).where(Event.session_id == session_id)
        )
    ).scalar()
    if count:
        return "session_start must be the first event; session already has events"
    return None


async def _check_session_started(
    db: AsyncSession,
    session_id: uuid.UUID,
    event_type: str,
) -> str | None:
    count = (
        await db.execute(
            select(func.count()).where(
                Event.session_id == session_id,
                Event.type == "session_start",
            )
        )
    ).scalar()
    if not count:
        return f"session_start must precede {event_type}"
    return None


async def _check_ref(
    db: AsyncSession,
    session_id: uuid.UUID,
    ref_type: str,
    ref_field: str,
    payload: dict[str, Any],
    event_type: str,
) -> str | None:
    ref_id = payload.get(ref_field)
    if not ref_id:
        return f"{event_type} is missing required field {ref_field!r}"

    count = (
        await db.execute(
            select(func.count()).where(
                Event.session_id == session_id,
                Event.type == ref_type,
                Event.payload[ref_field].as_string() == str(ref_id),
            )
        )
    ).scalar()
    if not count:
        return f"{event_type} references unknown {ref_field} {ref_id!r}"
    return None
