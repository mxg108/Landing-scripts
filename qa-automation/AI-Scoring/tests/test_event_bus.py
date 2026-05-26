"""Unit tests for the InMemoryEventBus pub/sub fanout."""

from __future__ import annotations

import asyncio

import pytest

from backend.services.event_bus import InMemoryEventBus


def _run(coro):
    return asyncio.run(coro)


def test_publish_delivers_to_single_subscriber():
    bus = InMemoryEventBus()
    q = bus.subscribe("member_support")

    async def go():
        await bus.publish("member_support", "eval_approved", {"agent": "Mich"})
        return await q.get()

    evt = _run(go())
    assert evt["event"] == "eval_approved"
    assert evt["data"] == {"agent": "Mich"}


def test_publish_fans_out_to_all_subscribers():
    """Two dashboards on the same team must both see the event."""
    bus = InMemoryEventBus()
    q1 = bus.subscribe("sales")
    q2 = bus.subscribe("sales")

    async def go():
        await bus.publish("sales", "eval_approved", {"score": 92})
        return await q1.get(), await q2.get()

    e1, e2 = _run(go())
    assert e1["data"]["score"] == 92
    assert e2["data"]["score"] == 92


def test_publish_isolated_per_team():
    """A publish to team A must NOT leak to a subscriber on team B."""
    bus = InMemoryEventBus()
    q_ms = bus.subscribe("member_support")
    q_sales = bus.subscribe("sales")

    async def go():
        await bus.publish("sales", "eval_approved", {"agent": "x"})
        # ms queue should be empty
        return q_ms.qsize(), await q_sales.get()

    ms_size, sales_evt = _run(go())
    assert ms_size == 0
    assert sales_evt["data"]["agent"] == "x"


def test_unsubscribe_stops_delivery():
    bus = InMemoryEventBus()
    q = bus.subscribe("member_support")
    bus.unsubscribe("member_support", q)

    async def go():
        await bus.publish("member_support", "eval_approved", {})
        return q.qsize()

    assert _run(go()) == 0


def test_publish_to_team_with_no_subscribers_is_noop():
    """A team nobody's watching must not raise — approval handler shouldn't
    care whether a dashboard happens to be open."""
    bus = InMemoryEventBus()

    async def go():
        await bus.publish("unknown_team", "eval_approved", {})

    _run(go())  # no exception → pass


def test_full_queue_drops_event_for_slow_subscriber():
    """A subscriber whose queue fills up loses subsequent events instead
    of back-pressuring the publisher. The publisher must not raise."""
    bus = InMemoryEventBus(queue_maxsize=2)
    q = bus.subscribe("member_support")

    async def go():
        await bus.publish("member_support", "e", {"i": 1})
        await bus.publish("member_support", "e", {"i": 2})
        await bus.publish("member_support", "e", {"i": 3})  # dropped
        return q.qsize()

    # Slow subscriber kept 2 events; the third was silently dropped.
    assert _run(go()) == 2


def test_unsubscribe_removes_correct_queue_among_multiple():
    bus = InMemoryEventBus()
    q1 = bus.subscribe("member_support")
    q2 = bus.subscribe("member_support")
    bus.unsubscribe("member_support", q1)

    async def go():
        await bus.publish("member_support", "e", {"v": 1})
        return q1.qsize(), q2.qsize()

    s1, s2 = _run(go())
    assert s1 == 0  # unsubscribed
    assert s2 == 1  # still subscribed
