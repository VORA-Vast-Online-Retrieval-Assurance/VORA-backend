"""In-process fan-out of live messages from research threads to WebSocket clients.

Research runs on worker threads; each WebSocket handler runs on the server's
event loop. ``publish`` is safe to call from any thread: it hands the message to
every subscriber's loop with ``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

# A client that stops reading loses the oldest messages first and is told to
# reload, instead of growing memory without bound.
MAX_PENDING = 500


class Subscription:
    def __init__(self, hub: "LiveHub", instance_id: str, loop: asyncio.AbstractEventLoop) -> None:
        self.hub = hub
        self.instance_id = instance_id
        self.loop = loop
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def _deliver(self, message: dict[str, Any]) -> None:
        if self.queue.qsize() >= MAX_PENDING:
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait({"type": "resync", "reason": "Client fell behind; reload the dataset"})
        self.queue.put_nowait(message)

    async def get(self, timeout: float | None = None) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(self.queue.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        self.hub.unsubscribe(self)


class LiveHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, set[Subscription]] = {}

    def subscribe(self, instance_id: str) -> Subscription:
        """Subscribe from a coroutine; messages arrive on the calling loop."""
        subscription = Subscription(self, instance_id, asyncio.get_running_loop())
        with self._lock:
            self._subscribers.setdefault(instance_id, set()).add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        with self._lock:
            group = self._subscribers.get(subscription.instance_id)
            if group:
                group.discard(subscription)
                if not group:
                    self._subscribers.pop(subscription.instance_id, None)

    def subscriber_count(self, instance_id: str) -> int:
        with self._lock:
            return len(self._subscribers.get(instance_id, ()))

    def publish(self, instance_id: str, message: dict[str, Any]) -> None:
        with self._lock:
            targets = list(self._subscribers.get(instance_id, ()))
        for subscription in targets:
            try:
                subscription.loop.call_soon_threadsafe(subscription._deliver, message)
            except RuntimeError:  # the subscriber's loop has closed
                self.unsubscribe(subscription)
