"""In-process pub/sub bus for live dashboard updates.

Today's deployment is single-replica (per `feedback_railway_isolation`),
so an in-memory `asyncio.Queue` fanout per team is enough. The
`EventBus` Protocol exists so a Redis (or Postgres LISTEN/NOTIFY) impl
can be swapped in by changing `get_event_bus()` alone — no caller
edits.

Lifecycle:
    bus = get_event_bus()
    queue = bus.subscribe("member_support")
    try:
        evt = await queue.get()            # blocks until a publish
        # render evt["event"], evt["data"]
    finally:
        bus.unsubscribe("member_support", queue)

Publish is `async` because future implementations (Redis) will be
network-bound; today's InMemory impl is sync under the hood but
preserves the contract.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, Protocol


# Event envelope shape pushed onto subscriber queues.
# `event` is the SSE event name (e.g. "eval_approved"); `data` is the
# JSON-serializable payload.
Event = dict[str, Any]


class EventBus(Protocol):
    async def publish(self, team_id: str, event: str, payload: dict) -> None: ...
    def subscribe(self, team_id: str) -> asyncio.Queue: ...
    def unsubscribe(self, team_id: str, queue: asyncio.Queue) -> None: ...


class InMemoryEventBus:
    """Per-team fanout. Each subscriber gets its own bounded queue.

    Bounded so a slow/disconnected subscriber can't grow memory without
    limit. `publish` uses `put_nowait` and DROPS events for any queue
    that's full — preferable to back-pressuring the approval handler
    while the dashboard is the bottleneck.
    """

    def __init__(self, queue_maxsize: int = 100) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._queue_maxsize = queue_maxsize

    async def publish(self, team_id: str, event: str, payload: dict) -> None:
        evt: Event = {"event": event, "data": payload}
        # Iterate a snapshot — subscribers may unsubscribe mid-publish if
        # their stream closes concurrently.
        for q in list(self._subscribers.get(team_id, ())):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                # Drop event for this slow subscriber. A correctly-behaving
                # SSE client drains in tight loop; if it can't keep up,
                # better to lose toasts than block the publisher.
                pass

    def subscribe(self, team_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._queue_maxsize)
        self._subscribers[team_id].add(q)
        return q

    def unsubscribe(self, team_id: str, queue: asyncio.Queue) -> None:
        self._subscribers.get(team_id, set()).discard(queue)


# Module-level singleton. Swap implementation by reassigning here (or by
# extending `get_event_bus` to read an env var) — no caller changes.
_bus: EventBus = InMemoryEventBus()


def get_event_bus() -> EventBus:
    return _bus
