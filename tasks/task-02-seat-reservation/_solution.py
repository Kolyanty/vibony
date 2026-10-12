"""
Reference solution (do NOT show to the candidate; private — excluded from the bundle).
Implements every requirement from task.md and spec.yaml (trap ids per the 3-trap set):
- atomic reserve under concurrency (a single shared per-item lock around the
  read-check-write — this is what closes trap 0, the oversell race),
- all-or-nothing reservation with no oversell,
- idempotent retries: dedup by request_id (trap 1),
- input validation: qty must be a positive integer (trap 2).

To QA the task by hand, copy the body of `reserve` below into app.py and fire many
concurrent reservations of 1 unit at a small stock (asyncio.gather) — the confirmed total
must equal the stock, never exceed it. The CI test backend/tests/test_task02_artifact.py
exercises this (plus idempotent retry and the qty guard) against this file.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict

from app import ReserveResult  # the return type from the stub
from store import Store

# One lock PER ITEM, shared across all callers and reused — this is what makes the
# read-check-write atomic. A fresh lock per call would synchronize nothing.
_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Idempotency: remember the result of each processed request_id so a retry returns the
# original outcome instead of reserving again (trap 1).
_seen: dict[str, ReserveResult] = {}


async def reserve(
    store: Store, item_id: str, qty: int, request_id: str
) -> ReserveResult:
    if not isinstance(qty, int) or qty <= 0:  # trap 2: validate before touching stock
        raise ValueError("qty must be a positive integer")

    if request_id in _seen:  # fast path: already applied
        return _seen[request_id]

    async with _locks[item_id]:  # serialize the critical section for this item
        if request_id in _seen:  # re-check inside the lock (concurrent retry)
            return _seen[request_id]
        current = await store.get(item_id)
        if current >= qty:
            await store.set(item_id, current - qty)
            result = ReserveResult(ok=True, remaining=current - qty)
        else:
            result = ReserveResult(ok=False, remaining=current)
        _seen[request_id] = result
        return result
