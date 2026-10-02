"""Shared reads of public sources: one read serves every track that needs the same page or listing.

Reading a source (opening it in a browser, or replaying a learned recipe) is the expensive part of a run. What it
produces, the raw observations, comes from a public page and does not depend on who asked: only the scoring does (each
track's columns and filters). So a read is stored once under its source key and reused by any track while it is fresh;
each track scores the shared rows with its own plan.

Two safeguards:
- **Freshness**: a read is reused only while younger than the asking batch allows (``max_age``): listings and pages
  have their own lifetimes, a live track accepts only reads newer than its own cadence, and a forced run accepts none.
- **One read at a time** ("single flight"): when a batch starts reading a source that another batch is already
  reading, it waits for that read instead of opening the page a second time, then uses its result.

Nothing private is shared: only rows from public pages, keyed by the page or listing they came from. Tracks, goals,
filters and conversations never enter this cache, and no one can see who else read a source.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from vora.shared.contracts import Observation

# How long a batch waits for another batch's read of the same source before reading it itself.
WAIT_SECONDS = 180


def page_key(url: str) -> str:
    from vora.learning.recipes import canonical_url

    return "page:" + canonical_url(url).split("#")[0].rstrip("/")


def recipe_key(section: str, recipe: Any) -> str:
    body = recipe.model_dump_json() if hasattr(recipe, "model_dump_json") else json.dumps(recipe, sort_keys=True)
    return f"recipe:{section}:{hashlib.sha256(body.encode()).hexdigest()[:16]}"


@dataclass(slots=True)
class CachedRead:
    key: str
    url: str
    final_url: str
    title: str
    status: int
    challenge: bool
    fetched_at: datetime
    rows: list[Observation]
    dataset_links: list[dict] = field(default_factory=list)
    file_links: list[dict] = field(default_factory=list)

    @property
    def age_seconds(self) -> float:
        return max(0.0, (datetime.now(UTC) - self.fetched_at).total_seconds())


class SourceCache:
    def __init__(self, repository) -> None:
        self.repository = repository
        self._lock = threading.Lock()
        self._reading: dict[str, threading.Event] = {}

    # --- reading and writing

    def lookup(self, key: str, max_age_seconds: float) -> CachedRead | None:
        if max_age_seconds <= 0:
            return None
        found = self.repository.get_source_read(key)
        if found is None:
            return None
        read = CachedRead(
            key=key, url=found["url"], final_url=found["final_url"], title=found["title"], status=found["status"],
            challenge=bool(found["challenge"]), fetched_at=datetime.fromisoformat(found["fetched_at"]),
            rows=[Observation.model_validate_json(row) for row in found["rows"]],
            dataset_links=found["meta"].get("dataset_links", []), file_links=found["meta"].get("file_links", []))
        return read if read.age_seconds <= max_age_seconds else None

    def store(self, key: str, *, url: str, final_url: str, title: str, status: int, challenge: bool,
              rows: list[Observation], dataset_links: list | None = None, file_links: list | None = None,
              merge: bool = False) -> None:
        """Keep a read for others. ``merge`` adds rows to what is stored (an incremental listing read that stopped at
        records it already had) instead of replacing it."""
        meta = {"dataset_links": [_plain(item) for item in dataset_links or []],
                "file_links": [_plain(item) for item in file_links or []]}
        self.repository.put_source_read(key, url=url, final_url=final_url, title=title, status=status,
                                        challenge=challenge, rows=[(row.id, row.model_dump_json()) for row in rows],
                                        meta=meta, merge=merge)

    def has(self, key: str) -> bool:
        return self.repository.get_source_read(key, rows=False) is not None

    # --- one read at a time

    def claim(self, key: str) -> threading.Event | None:
        """None when this caller now owns the read of ``key``; otherwise the event that fires when the current
        owner finishes (wait on it, then ``lookup`` again)."""
        with self._lock:
            event = self._reading.get(key)
            if event is not None:
                return event
            self._reading[key] = threading.Event()
            return None

    def release(self, key: str) -> None:
        with self._lock:
            event = self._reading.pop(key, None)
        if event is not None:
            event.set()

    def wait_for(self, key: str, max_age_seconds: float, deadline: float | None = None) -> CachedRead | None:
        """If another batch is reading ``key`` right now, wait for it and return its result (or None)."""
        with self._lock:
            event = self._reading.get(key)
        if event is None:
            return None
        budget = WAIT_SECONDS if deadline is None else max(0.0, min(WAIT_SECONDS, deadline - time.monotonic()))
        event.wait(budget)
        return self.lookup(key, max(max_age_seconds, WAIT_SECONDS + 60))


def _plain(item: Any) -> dict:
    return asdict(item) if hasattr(item, "__dataclass_fields__") else dict(item)


def age_text(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "less than a minute ago"
    if minutes < 60:
        return f"{minutes} min ago"
    return f"{minutes // 60} h {minutes % 60} min ago"
