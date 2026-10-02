"""A shared pool of browser workers.

Before, every batch started its own Chromium under one global lock: one batch
at a time, a process spawned and torn down per batch, and a second Chromium
for the interactive pass. Now every batch borrows page-sized turns from a
small pool:

* **Bounded.** At most ``size`` Chromium processes exist, however many
  batches (tracks) are running. A batch that needs a page while every worker
  is busy waits in a queue, ordered by priority and then arrival.
* **Lazy.** A worker (and its browser) starts only when work arrives and no
  worker is idle, so sequential work reuses one process.
* **Thread-correct.** Playwright's sync API belongs to the thread that started
  it, so a worker starts, uses and closes its own browser on its own thread.
* **Self-healing.** A browser that crashed is replaced on the next task, and a
  healthy one is recycled after ``recycle_after`` tasks so memory does not creep.
* **Isolated events.** A task runs with its batch's own event bus attached, so
  one shared browser serves many batches without mixing their events.

Concurrency here is *pages in flight* (one per worker), not tracks: hundreds of
tracks can be active while a few pages render at any moment.
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, TypeVar

logger = logging.getLogger("vora.browsers")
T = TypeVar("T")

# Priorities: lower runs first. Searching unblocks everything else in a batch.
SEARCH, PAGE, EXPLORE = 0, 1, 2

_BROKEN = ("target closed", "has been closed", "browser closed", "browser has been closed",
           "connection closed", "crashed", "target page, context or browser")


def looks_broken(exc: BaseException) -> bool:
    """Whether an error means the browser process itself is gone (not just one page failing)."""
    text = f"{type(exc).__name__} {exc}".casefold()
    return any(marker in text for marker in _BROKEN)


@dataclass(order=True)
class _Task:
    priority: int
    sequence: int
    fn: Callable[[Any], Any] | None = None
    future: Future | None = None


class PooledEngine:
    """What a batch uses instead of owning a browser: ``execute`` and ``explore``
    run on a pool worker, with this batch's events attached."""

    def __init__(self, pool: "BrowserPool", events: Any, priority: int = PAGE,
                 cancelled: threading.Event | None = None) -> None:
        self.pool, self.events, self.priority, self.cancelled = pool, events, priority, cancelled

    def _run(self, call: Callable[[Any], Any], priority: int | None = None) -> Any:
        def task(engine: Any) -> Any:
            original = engine.events
            engine.events = self.events
            try:
                return call(engine)
            finally:
                engine.events = original

        return self.pool.run(task, priority=self.priority if priority is None else priority,
                             cancelled=self.cancelled)

    def with_priority(self, priority: int) -> "PooledEngine":
        """The same batch's engine, queueing at another priority (e.g. searching first)."""
        return PooledEngine(self.pool, self.events, priority, self.cancelled)

    def execute(self, url: str, **kwargs: Any) -> Any:
        return self._run(lambda engine: engine.execute(url, **kwargs))

    def learn_structure(self, url: str, **kwargs: Any) -> Any:
        return self._run(lambda engine: engine.learn_structure(url, **kwargs), priority=EXPLORE)

    def run_recipe(self, url: str, recipe: Any, **kwargs: Any) -> Any:
        return self._run(lambda engine: engine.run_recipe(url, recipe, **kwargs))

    def explore(self, url: str, **kwargs: Any) -> Any:
        return self._run(lambda engine: engine.explore(url, **kwargs), priority=EXPLORE)

    def __enter__(self) -> "PooledEngine":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class BrowserPool:
    def __init__(self, factory: Callable[[], Any], size: int = 2, recycle_after: int = 200,
                 name: str = "vora-browser") -> None:
        if size < 1:
            raise ValueError("size must be at least 1")
        self._factory, self.size, self.recycle_after, self._name = factory, size, recycle_after, name
        self._queue: queue.PriorityQueue[_Task] = queue.PriorityQueue()
        self._sequence = itertools.count()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._idle = 0
        self._closed = False
        self.tasks_done = self.recycled = self.restarts = self.busy = 0

    # -- use ------------------------------------------------------------------

    def engine_for(self, events: Any, priority: int = PAGE, cancelled: threading.Event | None = None) -> PooledEngine:
        return PooledEngine(self, events, priority, cancelled)

    def run(self, fn: Callable[[Any], T], *, priority: int = PAGE, cancelled: threading.Event | None = None) -> T:
        """Run ``fn(engine)`` on a pool worker and return its result (or raise its error)."""
        future: Future = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("The browser pool is closed")
            self._queue.put(_Task(priority, next(self._sequence), fn, future))
            if self._idle == 0 and len(self._threads) < self.size:
                thread = threading.Thread(target=self._work, name=f"{self._name}-{len(self._threads) + 1}",
                                          daemon=True)
                self._threads.append(thread)
                thread.start()
        while True:
            try:
                return future.result(timeout=0.25)
            except TimeoutError:
                if cancelled is not None and cancelled.is_set() and future.cancel():
                    raise InterruptedError("Run cancelled") from None

    def close(self, timeout: float = 5.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            threads = list(self._threads)
        for _ in threads:
            self._queue.put(_Task(-1, next(self._sequence)))
        for thread in threads:
            thread.join(timeout)
        # Work still waiting will never run: tell its callers instead of leaving them waiting.
        while True:
            try:
                task = self._queue.get_nowait()
            except queue.Empty:
                break
            if task.future is not None and task.future.set_running_or_notify_cancel():
                task.future.set_exception(RuntimeError("The browser pool is closed"))

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"size": self.size, "started": len(self._threads), "busy": self.busy,
                    "queued": self._queue.qsize(), "tasks_done": self.tasks_done,
                    "recycled": self.recycled, "restarts": self.restarts}

    # -- worker ---------------------------------------------------------------

    def _open(self) -> Any:
        engine = self._factory()
        engine.__enter__()
        return engine

    @staticmethod
    def _shut(engine: Any) -> None:
        try:
            engine.__exit__(None, None, None)
        except Exception:
            logger.warning("A browser did not close cleanly", exc_info=True)

    def _work(self) -> None:
        engine: Any = None
        served = 0
        try:
            while True:
                with self._lock:
                    self._idle += 1
                task = self._queue.get()
                with self._lock:
                    self._idle -= 1
                if task.fn is None:  # stop signal
                    return
                if task.future is not None and not task.future.set_running_or_notify_cancel():
                    continue  # cancelled while waiting
                with self._lock:
                    self.busy += 1
                try:
                    if engine is not None and served >= self.recycle_after:
                        self._shut(engine)
                        engine, served = None, 0
                        with self._lock:
                            self.recycled += 1
                    if engine is None:
                        engine = self._open()
                    result = task.fn(engine)
                    served += 1
                    with self._lock:
                        self.tasks_done += 1
                    task.future.set_result(result)
                except BaseException as exc:  # noqa: BLE001 - reported to the caller
                    if engine is not None and looks_broken(exc):
                        logger.warning("Browser replaced after: %s", exc)
                        self._shut(engine)
                        engine, served = None, 0
                        with self._lock:
                            self.restarts += 1
                    task.future.set_exception(exc)
                finally:
                    with self._lock:
                        self.busy -= 1
        finally:
            if engine is not None:
                self._shut(engine)
