"""Thread-safe lifecycle hooks shared by engine consumers."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from threading import RLock
from types import MappingProxyType
from typing import Callable, Mapping

logger = logging.getLogger("vora.core.events")


class LifecycleEvent(str, Enum):
    EXECUTION_START = "onExecutionStart"
    NETWORK_REQUEST = "onNetworkRequest"
    NETWORK_RESPONSE = "onNetworkResponse"
    NETWORK_IDLE = "onNetworkIdle"
    EXECUTION_COMPLETE = "onExecutionComplete"
    EXECUTION_FAILED = "onExecutionFailed"


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    name: LifecycleEvent
    execution_id: str
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    data: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))


EventListener = Callable[[RuntimeEvent], None]


class EventBus:
    """Synchronous event delivery with isolated listener failures."""

    def __init__(self) -> None:
        self._listeners: dict[LifecycleEvent, list[EventListener]] = defaultdict(list)
        self._lock = RLock()

    def subscribe(self, event: LifecycleEvent, listener: EventListener) -> Callable[[], None]:
        with self._lock:
            if listener not in self._listeners[event]:
                self._listeners[event].append(listener)

        def unsubscribe() -> None:
            self.unsubscribe(event, listener)

        return unsubscribe

    def unsubscribe(self, event: LifecycleEvent, listener: EventListener) -> None:
        with self._lock:
            listeners = self._listeners.get(event, [])
            if listener in listeners:
                listeners.remove(listener)

    def emit(self, event: RuntimeEvent) -> None:
        with self._lock:
            listeners = tuple(self._listeners.get(event.name, ()))
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                logger.exception("Lifecycle listener failed for %s", event.name.value)

