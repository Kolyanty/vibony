"""
Backing inventory store for the reservation service.

Stands in for a database: each accessor is `async` and yields to the event loop
(`await`) before returning, exactly as a real DB round-trip would. The accessors are
deliberately low-level, non-atomic primitives — `get` then `set` is a read-modify-write
with a real suspension point in between. Enforcing the service's invariants across
concurrent callers is the reservation logic's job, not the store's.

You do not need to modify this file.
"""
from __future__ import annotations

import asyncio

# A small simulated I/O latency. It does two things: makes the `await` a real
# suspension point (so concurrent callers genuinely interleave, as they would against a
# database), and widens that window enough that a missing concurrency guard is
# observable, not a one-in-a-thousand flake.
_IO_LATENCY_S = 0.01


class Store:
    def __init__(self, stock: dict[str, int]) -> None:
        self._stock: dict[str, int] = dict(stock)

    async def get(self, item_id: str) -> int:
        """Available units for `item_id` (0 if unknown). Async: one DB read."""
        await asyncio.sleep(_IO_LATENCY_S)
        return self._stock.get(item_id, 0)

    async def set(self, item_id: str, units: int) -> None:
        """Overwrite available units for `item_id`. Async: one DB write."""
        await asyncio.sleep(_IO_LATENCY_S)
        self._stock[item_id] = units

    def snapshot(self) -> dict[str, int]:
        """Synchronous read of the whole store — for tests/printing, not the hot path."""
        return dict(self._stock)
