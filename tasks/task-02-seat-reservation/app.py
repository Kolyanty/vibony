"""
Seat / inventory reservation service.

Implement `reserve(...)` below per the requirements in task.md. The backing `Store`
(store.py) is given — you only write the reservation logic. This module has no HTTP
layer on purpose: you implement and verify the core logic directly (see task.md for how
to run and verify it), the way the service's request handler would call it.
"""
from __future__ import annotations

from dataclasses import dataclass

from store import Store


@dataclass
class ReserveResult:
    ok: bool          # was the full quantity reserved?
    remaining: int    # available units for the item after this call


async def reserve(
    store: Store, item_id: str, qty: int, request_id: str
) -> ReserveResult:
    """
    Reserve `qty` units of `item_id`. See task.md for the requirements.

    `request_id` identifies the client request — the same request may arrive more than
    once because clients retry on a timeout.

    Returns ReserveResult(ok=..., remaining=...).
    """
    raise NotImplementedError("Implement me")
