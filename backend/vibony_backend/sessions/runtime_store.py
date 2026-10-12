"""Private session checkpoints. Never included in candidate REST/event payloads."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import select

from vibony_backend.db.engine import get_sessionmaker
from vibony_backend.db.models import Session


class PersistenceError(RuntimeError):
    """A session cannot safely continue without durable state."""


async def load_runtime(code: str) -> dict[str, Any] | None:
    async with get_sessionmaker()() as db:
        row = (await db.execute(select(Session).where(Session.code == code))).scalar_one_or_none()
        if row is None:
            raise PersistenceError("Session no longer exists")
        return deepcopy(row.agent_state)


async def save_runtime(code: str, state: dict[str, Any]) -> None:
    async with get_sessionmaker()() as db, db.begin():
        row = (await db.execute(select(Session).where(Session.code == code).with_for_update())).scalar_one_or_none()
        if row is None:
            raise PersistenceError("Session no longer exists")
        # Integrity failures are sticky even if another path saved an older checkpoint.
        previous = (row.agent_state or {}).get("integrity_errors", [])
        merged = deepcopy(state)
        merged["integrity_errors"] = sorted(set(previous + merged.get("integrity_errors", [])))
        row.agent_state = merged
