"""In-process pub/sub broker for WS sessions.

One global `broker` instance per process. Each WS session (candidate,
interviewer) subscribes via `subscribe()`, gets an asyncio.Queue and reads from
it in its own `_send_loop`. When an event is written to the DB, `publish()` is
called — every subscriber of the session receives the message.

Subscriber queues are **bounded** (`BROKER_QUEUE_MAXSIZE`). Previously a
slow/stuck consumer grew its queue without limit — a memory DoS. Now, on
overflow, `offer()` enqueues a `None` sentinel (the same stop signal as in
`_send_loop`), so that subscriber's send loop exits and the client reconnects,
replaying the missed events via `?last_seq=` (the event log is the source of
truth; the queue is only a live fan-out buffer). `publish()` never blocks, so one
stuck subscriber does not slow delivery to the others.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)

# Max buffered messages per subscriber before it's treated as a stuck/slow
# consumer. ~1000 frames is far above any healthy live session's backlog; hitting
# it means the socket isn't draining, so we close it (client re-syncs by seq).
# <= 0 → unbounded (asyncio.Queue semantics) — escape hatch, not recommended.
BROKER_QUEUE_MAXSIZE = int(os.getenv("VIBONY_BROKER_QUEUE_MAXSIZE", "1000"))


def offer(q: asyncio.Queue[Any], message: Any) -> bool:
    """Non-blocking enqueue with a close-on-overflow policy.

    Returns True when the message was enqueued. On overflow (the consumer is too
    slow / not reading) returns False after dropping the oldest buffered message
    and enqueuing the ``None`` stop-sentinel, so the consumer's send loop exits —
    the client then reconnects and replays the gap via ``?last_seq=``. Never
    blocks the publisher, so one stuck subscriber can't stall delivery for others.
    """
    try:
        q.put_nowait(message)
        return True
    except asyncio.QueueFull:
        # Drop the oldest buffered message to make room for the stop-sentinel.
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover - full queue, won't be empty
            pass
        try:
            q.put_nowait(None)
        except asyncio.QueueFull:  # pragma: no cover - we just made room
            pass
        return False


def offer_advisory(q: asyncio.Queue[Any], message: Any) -> bool:
    """Best-effort enqueue for advisory (non-canonical) frames.

    Unlike :func:`offer`, on a full queue it simply **drops** the message: it never
    evicts a buffered canonical event and never closes the consumer. Advisory
    frames (``timer_tick``) are superseded by the next one ~10 s later, so dropping
    one is harmless — whereas evicting a queued canonical event would force a
    needless reconnect/replay just to advance an advisory countdown.
    """
    try:
        q.put_nowait(message)
        return True
    except asyncio.QueueFull:
        return False


class Broker:
    def __init__(self) -> None:
        self._queues: dict[str, list[asyncio.Queue[Any]]] = defaultdict(list)

    def subscribe(self, session_code: str) -> asyncio.Queue[Any]:
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=BROKER_QUEUE_MAXSIZE)
        self._queues[session_code].append(q)
        return q

    def unsubscribe(self, session_code: str, q: asyncio.Queue[Any]) -> None:
        try:
            self._queues[session_code].remove(q)
        except ValueError:
            pass

    async def publish(self, session_code: str, message: dict[str, Any]) -> None:
        for q in list(self._queues[session_code]):
            if not offer(q, message):
                logger.warning(
                    "broker: subscriber overflow for session %s — closing slow consumer",
                    session_code,
                )

    async def publish_advisory(self, session_code: str, message: dict[str, Any]) -> None:
        """Publish an advisory (non-canonical, droppable) frame to all subscribers.

        On a full queue the frame is dropped rather than evicting a canonical event
        or closing the consumer (see :func:`offer_advisory`). Used for ``timer_tick``
        — an advisory countdown must not knock a slow consumer into a
        reconnect/replay.
        """
        for q in list(self._queues[session_code]):
            offer_advisory(q, message)


broker = Broker()
