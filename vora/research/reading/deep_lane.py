"""The interactive pass of a research batch ("deep lane").

It runs beside the fast pass on its own thread, with its own browser (the
Playwright sync API belongs to the thread that started it). The fast pass
hands it every page it read that was not blocked, emptiest first; the deep
lane re-opens each with ``BrowserEngine.explore`` (scrolling, tabs, "load
more", further pages, iframes, chart data), scores what that reveals, and
follows a few same-site links that match the request (focused crawl).

It never writes the snapshot: finished pages go to an outbox that the batch
thread applies, so the snapshot has a single writer.
"""

from __future__ import annotations

import itertools
import logging
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from urllib.parse import urlparse

from vora.browser.events import EventBus
from vora.browser.explore import ExploreHints
from vora.extraction.collector import ExtractionCollector, ExtractionResult
from vora.extraction.files import parse_file
from vora.extraction.semantics import content_tokens, similar
from vora.shared.contracts import GoalPlan, Observation
from vora.shared.regions import COUNTRY_NAMES

from vora.research.reading.crawl import SiteLink, rank_site_links

logger = logging.getLogger("vora.deep_lane")

# Responses from these kinds of endpoints are site machinery, never data.
_MACHINERY = re.compile(
    r"consent|cookie|onetrust|cookielaw|gdpr|privacy|analytics|collect|beacon|telemetry|tracking|"
    r"pixel|adserver|doubleclick|advert|gtm|tagmanager|segment|sentry|optimizely|hotjar|i18n|"
    r"translation|locale|manifest|feature[-_]?flag|config|settings|session|auth|login",
    re.I,
)
MAX_TERM_SCAN = 200_000


def request_words(plan: GoalPlan) -> set[str]:
    """Words a response must mention to be about the request (places excluded)."""
    places = {term.casefold() for term in plan.geography} | COUNTRY_NAMES
    words: set[str] = set()
    for term in [*plan.subject_terms, *plan.subject_heads]:
        words.update(word for word in content_tokens(term) if word not in places and len(word) >= 3)
    for concept in plan.required_concepts:
        if concept.kind != "time":
            for name in [concept.name, *concept.aliases]:
                words.update(word for word in content_tokens(name) if len(word) >= 3)
    words.discard("value")
    return words


def relevant_response(url: str, body: bytes, words: set[str]) -> bool:
    """Chart data about the request mentions its words; site machinery does not."""
    if _MACHINERY.search(urlparse(url).path):
        return False
    if not words:
        return True
    text = body[:MAX_TERM_SCAN].decode("utf-8", "replace")
    found = set(content_tokens(text))
    return any(word in found or any(similar(word, token) >= 0.9 for token in found if token[:3] == word[:3])
               for word in words)


PAGE_BUDGET_SECONDS = 30.0
MIN_PAGE_SECONDS = 10.0
HOST_GAP_SECONDS = 2.0


def build_hints(plan: GoalPlan, forms: bool = True) -> ExploreHints:
    """What a search form may be filled with, taken from the request and nothing else.

    The window is the request's time window; keywords are its subject words
    without the name of the site it points at ("example" names where to look,
    not what to search for); places are its geography and entities.
    """
    window = None
    if plan.time_scope:
        try:
            window = (date.fromisoformat(plan.time_scope.start[:10]), date.fromisoformat(plan.time_scope.end[:10]))
        except ValueError:
            window = None
    named = {mention.casefold() for mention in plan.source_mentions}
    keywords = tuple(term for term in plan.subject_terms if term.casefold() not in named)
    return ExploreHints(window=window, keywords=keywords[:4],
                        places=tuple([*plan.geography, *plan.entities][:6]), forms=forms)


class HostGate:
    """At most one navigation per host every ``gap`` seconds, across both passes."""

    def __init__(self, gap: float = HOST_GAP_SECONDS) -> None:
        self.gap = gap
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = (urlparse(url).hostname or "").removeprefix("www.")
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next.get(host, 0.0))
            self._next[host] = slot + self.gap
        if slot > now:
            time.sleep(slot - now)


@dataclass(frozen=True, slots=True)
class DeepItem:
    url: str
    origin: str = "search"
    linked_from: str | None = None
    depth: int = 0
    anchor: str = ""


@dataclass(slots=True)
class DeepResult:
    item: DeepItem
    pages: list[ExtractionResult] = field(default_factory=list)
    captured: list[tuple[list[Observation], list[Observation], list[Observation], list[Observation]]] = \
        field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    links: list[SiteLink] = field(default_factory=list)
    title: str = ""
    elapsed: float = 0.0
    error: str | None = None


class DeepLane:
    def __init__(self, *, plan: GoalPlan, make_engine: Callable[[EventBus], object],
                 make_collector: Callable[[EventBus], ExtractionCollector], gate: HostGate,
                 remaining: Callable[[], float], cancelled: threading.Event, exclude: set[str],
                 crawl_per_site: int, window: tuple[str, str] | None, places: list[str],
                 forms: bool = True,
                 on_detail: Callable[[str], object] | None = None) -> None:
        self.plan = plan
        self._make_engine, self._make_collector = make_engine, make_collector
        self.gate = gate
        self.remaining = remaining
        self.cancelled = cancelled
        self.exclude = {url.rstrip("/") for url in exclude}
        self.crawl_per_site = crawl_per_site
        self.window, self.places = window, places
        self.hints = build_hints(plan, forms)
        self.on_detail = on_detail or (lambda detail: None)
        self.claimed: set[str] = set()
        self.explored = 0
        self.crawled = 0
        self._queued: set[str] = set()
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._order = itertools.count()
        self._outbox: queue.Queue[DeepResult] = queue.Queue()
        self._finished = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="vora-deep", daemon=True)

    # -- control (batch thread) -------------------------------------------------

    def start(self) -> "DeepLane":
        self._thread.start()
        return self

    def offer(self, url: str, priority: int, origin: str = "search", linked_from: str | None = None,
              depth: int = 0, anchor: str = "") -> bool:
        key = url.rstrip("/")
        with self._lock:
            if key in self._queued:
                return False
            self._queued.add(key)
        self._queue.put((priority, next(self._order), DeepItem(url, origin, linked_from, depth, anchor)))
        return True

    def is_claimed(self, url: str) -> bool:
        with self._lock:
            return url.rstrip("/") in self.claimed

    def finish(self) -> None:
        """No more pages from the fast pass; stop once the queue is empty."""
        self._finished.set()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float) -> None:
        self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def drain(self) -> list[DeepResult]:
        results = []
        while True:
            try:
                results.append(self._outbox.get_nowait())
            except queue.Empty:
                return results

    # -- work (deep thread) -----------------------------------------------------

    def _halted(self) -> bool:
        return self._stop.is_set() or self.cancelled.is_set() or self.remaining() < MIN_PAGE_SECONDS / 2

    def _run(self) -> None:
        events = EventBus()
        collector = self._make_collector(events)
        collector.begin(self.plan)
        engine = None
        try:
            while not self._halted():
                try:
                    _, _, item = self._queue.get(timeout=0.25)
                except queue.Empty:
                    if self._finished.is_set():
                        break
                    continue
                if self.remaining() < MIN_PAGE_SECONDS:
                    break
                if engine is None:
                    # The second browser starts only once there is a page for it.
                    engine = self._make_engine(events)
                    engine.__enter__()
                with self._lock:
                    self.claimed.add(item.url.rstrip("/"))
                self._outbox.put(self._explore(engine, collector, item))
        except Exception as exc:  # a failed second browser must not fail the batch
            logger.warning("Interactive pass stopped: %s", exc)
            self._outbox.put(DeepResult(DeepItem(""), error=f"Interactive pass stopped: {exc}"))
        finally:
            if engine is not None:
                try:
                    engine.__exit__(None, None, None)
                except Exception:
                    logger.warning("Interactive browser did not close cleanly", exc_info=True)
            collector.close()

    def _explore(self, engine, collector: ExtractionCollector, item: DeepItem) -> DeepResult:
        host = urlparse(item.url).netloc.removeprefix("www.")
        started = time.monotonic()
        self.on_detail(f"[deep] exploring {host}" + (" (linked page)" if item.origin == "crawl" else ""))
        self.gate.wait(item.url)
        try:
            exploration = engine.explore(
                item.url, budget_seconds=max(5.0, min(PAGE_BUDGET_SECONDS, self.remaining() - 5)),
                should_stop=self._halted, hints=self.hints)
        except Exception as exc:
            return DeepResult(item, error=f"{type(exc).__name__}: {str(exc)[:200]}",
                              elapsed=round(time.monotonic() - started, 3))
        self.explored += 1
        result = DeepResult(item, actions=list(exploration.actions),
                            title=exploration.states[0].title if exploration.states else "")
        result.pages = [collector.take(state.execution_id) for state in exploration.states]
        fetched = datetime.now(UTC).isoformat()
        words = request_words(self.plan)
        for response in exploration.captured:
            if not relevant_response(response.url, response.body, words):
                continue
            try:
                parsed = parse_file(response.body, response.extension, url=item.url,
                                    title=f"{result.title or host} (chart data)", window=self.window,
                                    places=self.places, fetched_at=fetched)
            except Exception:
                continue  # not tabular: configuration, analytics, ...
            path = urlparse(response.url).path or response.url
            rows = [Observation(**{**observation.model_dump(exclude={"id"}), "method": "network_json",
                                   "extraction_confidence": 0.85,
                                   "context": f"Data the page loads from {path}"[:240],
                                   "context_kind": "caption", "block_id": f"json:{path}"[:120]})
                    for observation in parsed.observations]
            if rows:
                result.captured.append((rows, *collector.score(rows)))
        if item.depth == 0 and self.crawl_per_site > 0 and exploration.states:
            with self._lock:
                skip = self.exclude | self._queued
            result.links = rank_site_links(exploration.states[0].html, item.url, self.plan,
                                           limit=self.crawl_per_site, exclude=skip)
            for link in result.links:
                if self.offer(link.url, priority=2, origin="crawl", linked_from=item.url, depth=1,
                              anchor=link.text):
                    self.crawled += 1
        result.elapsed = round(time.monotonic() - started, 3)
        return result
