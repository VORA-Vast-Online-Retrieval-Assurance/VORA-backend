"""A bounded cache: least-recently-used eviction and an optional time to live.

Every in-process cache in VORA uses this instead of a bare dict, so memory
cannot grow without limit however long the server runs or however many tracks
it serves. Each cache states its own key, size and lifetime where it is made.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any

MISSING: Any = object()


class BoundedCache:
    def __init__(self, max_entries: int, ttl_seconds: float | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self.max_entries, self.ttl_seconds, self._clock = max_entries, ttl_seconds, clock
        self._entries: OrderedDict[Hashable, tuple[Any, float | None]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = self.evictions = 0

    def get(self, key: Hashable, default: Any = MISSING) -> Any:
        """The cached value, or ``default``. A read makes the entry most recently used."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                value, expires = entry
                if expires is None or expires > self._clock():
                    self._entries.move_to_end(key)
                    self.hits += 1
                    return value
                del self._entries[key]
            self.misses += 1
            return default

    def set(self, key: Hashable, value: Any) -> None:
        expires = self._clock() + self.ttl_seconds if self.ttl_seconds is not None else None
        with self._lock:
            self._entries[key] = (value, expires)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
                self.evictions += 1

    def __contains__(self, key: Hashable) -> bool:
        return self.get(key) is not MISSING

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"entries": len(self._entries), "max_entries": self.max_entries, "hits": self.hits,
                    "misses": self.misses, "evictions": self.evictions}
