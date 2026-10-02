"""Headless browser runtime: engine, contexts, events, interactive exploration."""

from .contracts import ExecutionResult, NetworkRecord
from .engine import BrowserEngine, EngineNotStartedError
from .events import EventBus, LifecycleEvent, RuntimeEvent
from .settings import EngineSettings

__all__ = [
    "BrowserEngine",
    "EngineNotStartedError",
    "EngineSettings",
    "EventBus",
    "ExecutionResult",
    "LifecycleEvent",
    "NetworkRecord",
    "RuntimeEvent",
]
