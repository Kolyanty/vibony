"""Bounded broker subscriber queues + close-on-overflow.

A slow/stuck consumer must not grow its queue without bound. ``offer`` enforces a
non-blocking enqueue: on overflow it drops the oldest frame, enqueues the ``None``
stop-sentinel (so the consumer's send loop exits and the client re-syncs by seq),
and returns False — the publisher never blocks.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_subscribe_queue_is_bounded() -> None:
    from vibony_backend.sessions.broker import BROKER_QUEUE_MAXSIZE, Broker

    b = Broker()
    q = b.subscribe("S-1")
    assert q.maxsize == BROKER_QUEUE_MAXSIZE
    assert BROKER_QUEUE_MAXSIZE > 0


async def test_offer_delivers_until_full() -> None:
    from vibony_backend.sessions.broker import offer

    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    assert offer(q, {"n": 1}) is True
    assert offer(q, {"n": 2}) is True
    # Full now: third offer overflows.
    assert offer(q, {"n": 3}) is False


async def test_offer_overflow_drops_oldest_and_enqueues_stop_sentinel() -> None:
    from vibony_backend.sessions.broker import offer

    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    offer(q, {"n": 1})
    offer(q, {"n": 2})
    offer(q, {"n": 3})  # overflow

    drained = [q.get_nowait(), q.get_nowait()]
    # Oldest ({"n": 1}) was dropped to make room; the None stop-sentinel is present.
    assert {"n": 1} not in drained
    assert None in drained
    assert q.empty()


async def test_publish_closes_overflowed_subscriber() -> None:
    from vibony_backend.sessions.broker import Broker

    b = Broker()
    small: asyncio.Queue = asyncio.Queue(maxsize=1)
    # Inject a deliberately tiny queue to exercise the overflow path on publish.
    b._queues["S-2"].append(small)

    await b.publish("S-2", {"x": 1})  # fills the queue
    await b.publish("S-2", {"x": 2})  # overflow → stop-sentinel

    # The slow consumer receives the None stop-sentinel so its send loop exits.
    assert small.get_nowait() is None


async def test_publish_does_not_block_on_full_subscriber() -> None:
    from vibony_backend.sessions.broker import Broker

    b = Broker()
    full: asyncio.Queue = asyncio.Queue(maxsize=1)
    b._queues["S-3"].append(full)

    # Even with many publishes against a never-draining subscriber, publish returns
    # promptly (no await blocks). A timeout means the publisher stalled on a slow
    # consumer — the exact DoS this fixes.
    for i in range(50):
        await asyncio.wait_for(b.publish("S-3", {"i": i}), timeout=1.0)


# ---------------------------------------------------------------------------
# Advisory publish must not evict canonical events
# ---------------------------------------------------------------------------


async def test_offer_advisory_drops_when_full_without_evicting() -> None:
    from vibony_backend.sessions.broker import offer_advisory

    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    assert offer_advisory(q, {"n": 1}) is True
    assert offer_advisory(q, {"n": 2}) is True
    # Full now: the advisory frame is dropped — nothing evicted, no sentinel added.
    assert offer_advisory(q, {"n": 3}) is False
    drained = [q.get_nowait(), q.get_nowait()]
    assert drained == [{"n": 1}, {"n": 2}]
    assert q.empty()


async def test_publish_advisory_preserves_buffered_canonical_event() -> None:
    from vibony_backend.sessions.broker import Broker

    b = Broker()
    small: asyncio.Queue = asyncio.Queue(maxsize=1)
    b._queues["S-4"].append(small)

    # A canonical event is buffered for a slow consumer.
    await b.publish("S-4", {"kind": "assistant_message", "seq": 7})
    # An advisory timer_tick arrives while the queue is full → it must be dropped,
    # NOT evict the canonical event nor inject the None stop-sentinel.
    await b.publish_advisory("S-4", {"kind": "timer_tick", "remaining_seconds": 42})

    assert small.get_nowait() == {"kind": "assistant_message", "seq": 7}
    assert small.empty()  # no stop-sentinel — the consumer is not forced to reconnect
