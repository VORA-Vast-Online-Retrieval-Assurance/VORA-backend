"""Background research orchestration across the core and extraction layers."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlparse

from vora.settings import settings
from vora.browser.engine import BrowserEngine
from vora.browser.settings import EngineSettings
from vora.browser.events import EventBus
from vora.extraction.collector import ExtractionCollector, ExtractionResult
from vora.extraction.scoring import ObservationScorer
from vora.extraction.datasets import DatasetLink, parse_csv, rank_links
from vora.extraction.files import AUTOMATIC as AUTOMATIC_FILES
from vora.extraction.files import MAX_FILES_PER_RUN, FoundFile, file_id, found_file, from_dataset_link
from vora.extraction.files import relevance as file_relevance
from vora.extraction.files import parse_file
from vora.extraction.parser import dataset_observations, unpivot_wide
from vora.extraction.requirements import upgrade_plan
from vora.extraction.temporal import today_utc
from vora.research.planning.source_resolver import name_columns, resolve_sites, usable_models
from vora.research.planning.provider import analyze_goal, available_models, configured_models, map_fields
from vora.learning.recipes import Recipe, RecipeRun, canonical_url
from vora.shared.contracts import FileLink, GoalPlan, Observation, ResearchSnapshot, SourceOutcome
from vora.shared.urls import domain_of, ensure_public_url, goal_domains
from vora.shared.system import available_memory_mb

from vora.research.discovery import search_api
from vora.research.reading.browser_pool import EXPLORE, PAGE, SEARCH, BrowserPool
from vora.output.datasets import download as download_dataset
from vora.research.reading.deep_lane import DeepLane, DeepResult, HostGate
from vora.research.reading.source_cache import CachedRead, SourceCache, age_text, page_key, recipe_key
from vora.output.datasets import download_file
from vora.research.discovery.discovery import SearchResult, discover
from vora.research.discovery.discovery import _search as search_for
from vora.research.discovery.knowledge import site_knowledge
from vora.research.discovery import source_health
from vora.learning.source_registry import for_url as registry_for_url
from vora.output.live_hub import LiveHub
from vora.research.discovery.ranking import RankedCandidate, rank, read_recently
from vora.storage.repository import Repository
from vora.output.tables import build_table, record_meta

logger = logging.getLogger("vora.coordinator")

# Pipeline phases in execution order. The API exposes this list so clients can
# render progress without inventing stages.
PHASES = ("queued", "planning", "discovery", "rendering", "extracting", "scoring", "merging",
          "complete")
TERMINAL = {"succeeded", "failed", "cancelled"}


def source_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def merge_dataset(snapshot: ResearchSnapshot) -> None:
    """De-duplicate observations and order the dataset by period, then score.

    An observation id lives in one bucket only (accepted before partial before
    rejected): two passes over the same page can judge a row differently.
    """
    placed: set[str] = set()
    for name in ("accepted", "partial", "rejected"):
        unique: dict[str, Observation] = {}
        for item in getattr(snapshot, name):
            if item.id not in placed:
                unique.setdefault(item.id, item)
        placed.update(unique)
        setattr(snapshot, name, list(unique.values()))
    seen: set[str] = set()
    snapshot.raw = [item for item in snapshot.raw if not (item.id in seen or seen.add(item.id))]
    snapshot.accepted.sort(key=lambda item: (item.period_start or "9999", item.source_domain, -item.score))
    snapshot.partial.sort(key=lambda item: -item.score)


class BrowserUnavailable(RuntimeError):
    """The configured browser cannot be started; the message says how to fix it."""


# Seconds between snapshot writes while a batch runs (a large snapshot is
# several MB of JSON); the final state is always written.
SAVE_INTERVAL = 5.0
# Share of the batch budget that searching may use, leaving time for pages.
DISCOVERY_SHARE = 0.4
# Order in which the interactive pass revisits pages: emptiest first.
DEEP_PRIORITY = {"empty": 0, "partial": 1, "complete": 3}


def request_key(goal: str) -> str:
    """A request as a cache key: lower case, punctuation and spacing ignored. Word order is kept ("from India to the
    US" is not "from the US to India"); requests that differ more still share every source they both read."""
    words = re.findall(r"[0-9a-z]+", goal.casefold())
    return hashlib.sha256(" ".join(words).encode()).hexdigest()[:32]


@dataclass
class Batch:
    """State of one research batch, owned by the thread running ``_execute``."""

    instance_id: str
    run_id: str
    plan: GoalPlan
    snapshot: ResearchSnapshot
    cancelled: threading.Event
    nav_seconds: float = 30.0
    started: float = field(default_factory=time.monotonic)
    deadline: float = math.inf
    raw_ids: set[str] = field(default_factory=set)
    scored_ids: set[str] = field(default_factory=set)
    dirty: bool = False
    saved_at: float = 0.0
    search_report: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Registry sources whose recipe already read their listing in this batch.
    recipes_done: set[str] = field(default_factory=set)
    # Official sites (found by the resolver) whose learned listing gave records in this batch.
    learned_done: set[str] = field(default_factory=set)
    official: set[str] = field(default_factory=set)         # hosts the resolver named for this request
    learned_keys: set[str] = field(default_factory=set)      # sections already read this batch
    # Politeness shared by the fast and interactive passes.
    gate: HostGate = field(default_factory=HostGate)
    # How old a shared read of a source may be for this batch (seconds): listings and ordinary pages.
    max_age_listing: float = 0.0
    max_age_page: float = 0.0
    reused: int = 0                                           # sources served from a shared read
    lane: DeepLane | None = None
    deep_rows: int = 0

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def start_clock(self, seconds: float) -> None:
        self.started = time.monotonic()
        self.deadline = self.started + seconds

    def out_of_time(self) -> bool:
        """No time left to finish another page before the deadline."""
        return time.monotonic() >= self.deadline - (self.nav_seconds + 5)

    def check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise InterruptedError("Run cancelled")


def summarize_search(report: list[str]) -> str:
    """"duckduckgo: 3 ok, 1 blocked; bing: 1 ok" from per-query notes."""
    counts: dict[str, Counter] = {}
    for note in report:
        engine, _, outcome = note.partition(": ")
        kind = ("blocked" if "blocked" in outcome else "consent" if "consent" in outcome
                else "empty" if "no results" in outcome else "skipped" if "skipped" in outcome
                else "ok" if outcome.endswith("results") else "failed")
        counts.setdefault(engine, Counter())[kind] += 1
    return "; ".join(f"{engine}: " + ", ".join(f"{n} {kind}" for kind, n in tally.items())
                     for engine, tally in counts.items())



def learn_key(url: str) -> str:
    """What a learned recipe belongs to: a section of a site (host and path), so two sections of one site each get theirs."""
    parts = urlparse(canonical_url(url))       # a session id kept in the path is not part of the section
    return ((parts.hostname or "").removeprefix("www.") + parts.path.rstrip("/")).lower()[:200]


class ResearchCoordinator:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self._pool = ThreadPoolExecutor(max_workers=max(1, settings.max_concurrent_batches),
                                        thread_name_prefix="vora-research")
        self._cancel: dict[str, threading.Event] = {}
        # Browsers are shared by every batch (see vora.research.reading.browser_pool), and the
        # politeness gate is process-wide: two tracks reading one site are spaced apart.
        self._browsers: BrowserPool | None = None
        self._browsers_key: object = None
        self._browsers_lock = threading.Lock()
        self.gate = HostGate()
        self._active_batches = 0
        self._active_lock = threading.Lock()
        self._scheduler: threading.Thread | None = None
        self.sources = SourceCache(repository)
        self._fresh_runs: set[str] = set()                              # runs that must not reuse shared reads
        self._stopping = threading.Event()
        # Live updates for WebSocket clients: run changes and new rows.
        self.hub = LiveHub()
        repository.observe_runs(self._on_run_change)

    def _on_run_change(self, run: dict, event: dict | None) -> None:
        instance_id = run.get("instance_id")
        if not instance_id or not self.hub.subscriber_count(instance_id):
            return
        self.hub.publish(instance_id, {"type": "run", "run": run})
        if event:
            self.hub.publish(instance_id, {"type": "run_event", "event": event})

    # -- run control --------------------------------------------------------

    def submit(self, instance_id: str, fresh: bool = False) -> dict:
        """Queue a run. A newer run replaces any active run of the same instance. ``fresh`` reads every source again
        instead of reusing a recent shared read."""
        for active in self.repository.active_runs(instance_id):
            self.cancel(active["id"], "Superseded by a newer run")
        run = self.repository.create_run(instance_id)
        if fresh:
            self._fresh_runs.add(run["id"])
        event = threading.Event()
        self._cancel[run["id"]] = event
        self._pool.submit(self._execute, instance_id, run["id"], event)
        return run

    def cancel(self, run_id: str, reason: str = "Cancellation requested") -> dict | None:
        event = self._cancel.get(run_id)
        if event:
            event.set()
        run = self.repository.get_run(run_id)
        if run and run["status"] in TERMINAL:
            return run
        if run and not event:
            # Not owned by this process (e.g. the server restarted mid-run).
            return self.repository.update_run(run_id, status="cancelled", phase="cancelled",
                                              detail=reason, cancellation_requested=True,
                                              finished_at=datetime.now(UTC).isoformat())
        return self.repository.update_run(run_id, cancellation_requested=True, detail=reason)

    def recover_orphans(self) -> None:
        """Mark runs left active by a previous process as failed."""
        for run in self.repository.active_runs():
            if run["id"] not in self._cancel:
                self.repository.update_run(run["id"], status="failed", phase="failed",
                                           detail="Interrupted by a server restart",
                                           error="Interrupted by a server restart",
                                           finished_at=datetime.now(UTC).isoformat())

    def _engine_settings(self) -> EngineSettings:
        return EngineSettings.from_env()

    def _browser_settings(self) -> EngineSettings | None:
        """Validated browser settings, or ``BrowserUnavailable`` with the fix."""
        try:
            engine_settings = self._engine_settings()
            return engine_settings.validate() if engine_settings is not None else None
        except (ValueError, FileNotFoundError) as exc:
            raise BrowserUnavailable(str(exc)) from None

    def readiness(self) -> dict:
        free = available_memory_mb()
        common = {
            "browsers": self.pool_stats(),
            "llm_models": configured_models(), "llm_available": available_models(),
            "search": search_api.configured() or "browser",
            "free_memory_mb": free,
            # The interactive pass needs a second browser (see deep_lane_min_free_mb).
            "interactive_pass": settings.deep_lane and (free is None or free >= settings.deep_lane_min_free_mb),
        }
        try:
            engine = self._engine_settings().validate()
            return {"ready": True, "headless": engine.headless,
                    "binary_path": str(engine.binary_path.resolve()), **common}
        except Exception as exc:
            return {"ready": False, "headless": True, "reason": str(exc), **common}

    # -- live scheduling ----------------------------------------------------

    def start_scheduler(self) -> None:
        if self._scheduler and self._scheduler.is_alive():
            return
        self._stopping.clear()
        self._scheduler = threading.Thread(target=self._schedule_loop, name="vora-live",
                                           daemon=True)
        self._scheduler.start()

    def stop_scheduler(self) -> None:
        self._stopping.set()

    def shutdown(self) -> None:
        """Stop scheduling and ask active runs to stop at their next checkpoint.

        Python waits for research threads before exiting, so without this a
        run in progress would keep the process alive until it finished.
        """
        self.stop_scheduler()
        active = list(self._cancel)
        if active:
            print(f"Stopping {len(active)} active run(s) after the current step...", flush=True)
        for run_id in active:
            self.cancel(run_id, "Server stopped")
        self._pool.shutdown(wait=False, cancel_futures=True)
        self.close_browsers()
        # A fresh pool keeps the coordinator usable if the app is started again
        # in the same process (tests); it starts no threads until a submit.
        self._pool = ThreadPoolExecutor(max_workers=max(1, settings.max_concurrent_batches),
                                        thread_name_prefix="vora-research")

    # -- shared browsers ----------------------------------------------------------

    def _browsers_for(self, engine_settings: EngineSettings | None) -> BrowserPool:
        """The shared browser pool for these settings (replaced if the settings changed)."""
        with self._browsers_lock:
            if self._browsers is None or self._browsers_key != engine_settings:
                stale = self._browsers
                self._browsers = BrowserPool(
                    lambda: BrowserEngine(engine_settings, EventBus()), size=max(1, settings.browser_pool_size),
                    recycle_after=max(1, settings.browser_recycle_after))
                self._browsers_key = engine_settings
                if stale is not None:
                    threading.Thread(target=stale.close, daemon=True).start()
            return self._browsers

    def close_browsers(self) -> None:
        with self._browsers_lock:
            pool, self._browsers, self._browsers_key = self._browsers, None, None
        if pool is not None:
            pool.close(timeout=0.5)

    @contextmanager
    def _batch_slot(self):
        """Counts batches in flight (for readiness and observability); does not serialize them."""
        with self._active_lock:
            self._active_batches += 1
        try:
            yield
        finally:
            with self._active_lock:
                self._active_batches -= 1

    def pool_stats(self) -> dict:
        pool = self._browsers
        with self._active_lock:
            active = self._active_batches
        return {"active_batches": active, "max_concurrent_batches": settings.max_concurrent_batches,
                **(pool.stats() if pool else {"size": settings.browser_pool_size, "started": 0, "busy": 0,
                                              "queued": 0, "tasks_done": 0, "recycled": 0, "restarts": 0})}

    def next_cycle_at(self, instance_id: str, live_enabled: bool) -> datetime | None:
        """When the next live batch starts: ``live_interval_seconds`` after the
        previous batch *started*, and never before it finished. A live track
        without any batch starts at once."""
        if not live_enabled:
            return None
        latest = self.repository.latest_run(instance_id)
        if latest is None:
            return datetime.now(UTC)
        if latest["status"] not in TERMINAL:
            return None
        started = datetime.fromisoformat(latest["started_at"] or latest["created_at"])
        due = started + timedelta(seconds=settings.live_interval_seconds)
        if latest["finished_at"]:
            due = max(due, datetime.fromisoformat(latest["finished_at"]))
        return due

    def purge_history(self) -> dict[str, int]:
        """Drop old run events, runs, source history and cached searches (bounded growth)."""
        removed = self.repository.purge(event_days=settings.retention_event_days,
                                        run_days=settings.retention_run_days,
                                        history_days=settings.retention_history_days)
        if any(removed.values()):
            logger.info("Purged old history: %s", removed)
        return removed

    def _schedule_loop(self) -> None:
        last_purge = 0.0
        last_sync = float("-inf")
        source_health.import_blacklist(self.repository)
        while not self._stopping.wait(5):
            try:
                source_health.import_blacklist(self.repository, only_if_changed=True)   # a hand edit is live at once
                if time.monotonic() - last_sync > settings.sync_minutes * 60:
                    last_sync = time.monotonic()
                    source_health.sync(self.repository)
                if time.monotonic() - last_purge > 3600:
                    last_purge = time.monotonic()
                    self.purge_history()
                now = datetime.now(UTC)
                for instance in self.repository.live_instances():
                    due = self.next_cycle_at(instance["id"], True)
                    if due is not None and due <= now:
                        logger.info("Starting live cycle for %s", instance["id"])
                        self.submit(instance["id"])
            except Exception:
                logger.exception("Live scheduler iteration failed")

    # -- on-demand file extraction ------------------------------------------

    def extract_file(self, instance_id: str, identifier: str) -> FileLink:
        """Download one registered file into memory, parse it and merge its rows.

        Raises ``LookupError`` (unknown file), ``PermissionError`` (never
        fetched, e.g. programs), ``RuntimeError`` (a run is active) or the
        download/parse error, which is also recorded on the file.
        """
        snapshot = self.repository.get_snapshot(instance_id)
        entry = next((item for item in snapshot.files if item.id == identifier), None) if snapshot else None
        if entry is None:
            raise LookupError("File not found")
        if not entry.extractable:
            raise PermissionError(entry.reason or f".{entry.extension} files are kept as links only")
        if self.repository.active_runs(instance_id):
            raise RuntimeError("Wait for the active run to finish before extracting files")
        plan = snapshot.plan
        window = (plan.time_scope.start, plan.time_scope.end) if plan.time_scope else None
        host = domain_of(entry.url)
        started = time.monotonic()
        try:
            downloaded = download_file(entry.url)
            parsed = parse_file(downloaded.content, entry.extension, url=entry.url,
                                title=entry.title or entry.name, window=window,
                                places=[*plan.geography, *plan.entities],
                                fetched_at=downloaded.fetched_at, modified_at=downloaded.modified_at)
        except Exception as exc:
            self._mark_file(snapshot, entry.url, status="failed", reason=str(exc)[:300])
            self.repository.save_snapshot(instance_id, snapshot)
            raise
        accepted, partial, rejected = ObservationScorer(
            plan, block_support=settings.block_support).score_all(parsed.observations)
        self._forget_source(snapshot, entry.url)
        snapshot.raw.extend(parsed.observations)
        snapshot.accepted.extend(accepted)
        snapshot.partial.extend(partial)
        snapshot.rejected.extend(rejected)
        snapshot.sources.append(SourceOutcome(
            id=source_id(entry.url), url=entry.url, title=f"{entry.title or entry.name} (file)",
            status="complete" if accepted else "partial", extracted=len(parsed.observations),
            accepted=len(accepted), partial=len(partial), rejected=len(rejected),
            elapsed_seconds=round(time.monotonic() - started, 3), domain=host,
            modified_at=downloaded.modified_at, fetched_at=downloaded.fetched_at, origin="file",
            linked_from=entry.linked_from, reason=f"Extracted on request · {parsed.note}",
        ))
        self._mark_file(snapshot, entry.url, status="extracted", reason=parsed.note,
                        extracted_rows=len(parsed.observations), accepted_rows=len(accepted),
                        size_bytes=len(downloaded.content), content_type=downloaded.content_type,
                        extracted_at=datetime.now(UTC))
        self.repository.record_source(instance_id, None, entry.url, host,
                                      "complete" if accepted else "partial", len(parsed.observations), len(accepted))
        merge_dataset(snapshot)
        snapshot.scored_at = datetime.now(UTC)
        self.repository.save_snapshot(instance_id, snapshot)
        return next(item for item in snapshot.files if item.id == identifier)

    # -- rescoring ------------------------------------------------------------

    def rescore(self, instance_id: str) -> ResearchSnapshot | None:
        """Re-run semantic scoring over stored raw observations without fetching."""
        instance = self.repository.get_instance(instance_id)
        snapshot = self.repository.get_snapshot(instance_id)
        if not instance or snapshot is None:
            return None
        plan = snapshot.plan if snapshot.plan.concepts else upgrade_plan(snapshot.plan, goal=instance["goal"])
        titles = {source.url: source.title for source in snapshot.sources}
        raw = [
            row for item in snapshot.raw
            for row in unpivot_wide(item.model_copy(update={
                "source_title": item.source_title or titles.get(item.source_url, "")}))
        ]
        accepted, partial, rejected = ObservationScorer(plan, block_support=settings.block_support).score_all(raw)
        snapshot.plan, snapshot.raw = plan, raw
        snapshot.accepted, snapshot.partial, snapshot.rejected = accepted, partial, rejected
        merge_dataset(snapshot)
        self._recount_sources(snapshot)
        snapshot.scored_at = datetime.now(UTC)
        self.repository.save_snapshot(instance_id, snapshot)
        return snapshot

    @staticmethod
    def _recount_sources(snapshot: ResearchSnapshot) -> None:
        for source in snapshot.sources:
            if source.status in {"failed", "skipped"}:
                continue
            source.extracted = sum(1 for item in snapshot.raw if item.source_url == source.url)
            source.accepted = sum(1 for item in snapshot.accepted if item.source_url == source.url)
            source.partial = sum(1 for item in snapshot.partial if item.source_url == source.url)
            source.rejected = sum(1 for item in snapshot.rejected if item.source_url == source.url)
            source.domain = source.domain or urlparse(source.url).netloc.removeprefix("www.")
            challenged = any(item.content_role == "challenge" for item in snapshot.rejected
                             if item.source_url == source.url)
            if challenged and not source.accepted:
                source.status = "blocked"
                source.reason = source.reason or "Security verification page blocked the content"
            else:
                source.status = "complete" if source.accepted else "partial" if source.partial else "empty"

    # -- execution ----------------------------------------------------------

    def _field_mapper(self, plan: GoalPlan):
        if not settings.semantic_llm or not available_models():
            return None
        return lambda headers, concepts: map_fields(headers, concepts, plan.normalized_goal)

    def _plan(self, instance: dict) -> GoalPlan:
        """This track's plan: reused from today's earlier batch of the same goal
        when a language model made it, so frequent batches skip the model."""
        previous = self.repository.get_snapshot(instance["id"])
        goal = " ".join(instance["goal"].split())
        if previous is not None and previous.plan.normalized_goal.casefold() == goal.casefold()                 and previous.plan.planner.startswith("llm:") and previous.plan.concepts:
            scope = previous.plan.time_scope
            if scope is None or scope.resolved_on == today_utc().isoformat():
                return previous.plan
        key = request_key(goal)
        shared = self.repository.get_query_cache("plan", key, settings.query_cache_minutes)
        if shared:
            plan = GoalPlan.model_validate(shared)
            scope = plan.time_scope
            if scope is None or scope.resolved_on == today_utc().isoformat():
                return plan
        plan = analyze_goal(instance["goal"])
        if plan.planner.startswith("llm:"):
            self.repository.put_query_cache("plan", key, plan.model_dump(mode="json"))
        return plan

    def _start_snapshot(self, instance_id: str, plan: GoalPlan) -> tuple[ResearchSnapshot, bool]:
        """Continue the previous snapshot when the goal is unchanged, else start fresh.

        Merging keeps rows from pages not re-read this run; pages that are
        re-read replace their old rows (see ``_forget_source``).
        """
        previous = self.repository.get_snapshot(instance_id)
        same_goal = previous is not None and \
            previous.plan.normalized_goal.casefold() == plan.normalized_goal.casefold()
        if settings.merge_runs and same_goal:
            return previous.model_copy(update={
                "plan": plan, "outcome": "running",
                # Last run's queue is rebuilt from this run's ranking.
                "sources": [source for source in previous.sources if source.status != "skipped"],
            }), True
        return ResearchSnapshot(plan=plan), False

    @staticmethod
    def _forget_source(snapshot: ResearchSnapshot, *urls: str) -> None:
        """Drop stored rows and outcome of a source that is being read again.

        Pass the requested and the final (redirected) URL: rows are stored
        under the final one, the outcome under the requested one.
        """
        gone = {url for url in urls if url}
        for name in ("raw", "accepted", "partial", "rejected"):
            setattr(snapshot, name, [item for item in getattr(snapshot, name) if item.source_url not in gone])
        snapshot.sources = [source for source in snapshot.sources if source.url not in gone]

    def _forget(self, batch: Batch, *urls: str) -> None:
        self._forget_source(batch.snapshot, *urls)
        batch.raw_ids = {item.id for item in batch.snapshot.raw}
        batch.scored_ids = {item.id for name in ("accepted", "partial", "rejected")
                            for item in getattr(batch.snapshot, name)}

    # -- applying results (single writer) -------------------------------------

    def _apply_rows(self, batch: Batch, raw: list[Observation], accepted: list[Observation],
                    partial: list[Observation], rejected: list[Observation], *, source_url: str,
                    lane: str = "fast") -> tuple[int, int, int, int]:
        """Add one page's results to the batch snapshot and stream them live.

        Rows already stored (the same page seen by both passes, or twice in one
        pass) are skipped. Returns how many raw, accepted, partial and rejected
        rows were new.
        """
        snapshot = batch.snapshot

        def new(items: list[Observation], seen: set[str]) -> list[Observation]:
            fresh = []
            for item in items:
                if item.id not in seen:
                    seen.add(item.id)
                    fresh.append(item)
            return fresh

        raw = new(raw, batch.raw_ids)
        accepted, partial, rejected = (new(items, batch.scored_ids) for items in (accepted, partial, rejected))
        snapshot.raw.extend(raw)
        snapshot.accepted.extend(accepted)
        snapshot.partial.extend(partial)
        snapshot.rejected.extend(rejected)
        batch.dirty = True
        if accepted or partial:
            self._publish_rows(batch, accepted, len(partial), source_url, lane)
        return len(raw), len(accepted), len(partial), len(rejected)

    def _publish_rows(self, batch: Batch, accepted: list[Observation], partial_added: int,
                      source_url: str, lane: str) -> None:
        if not self.hub.subscriber_count(batch.instance_id):
            return
        columns, rows = build_table(accepted, batch.plan)
        self.hub.publish(batch.instance_id, {
            "type": "rows", "run_id": batch.run_id, "lane": lane, "source_url": source_url,
            "columns": columns, "rows": rows, "records": [record_meta(item) for item in accepted],
            "partial_added": partial_added, "accepted_total": len(batch.snapshot.accepted),
        })

    def _save(self, batch: Batch, force: bool = False) -> None:
        """Write the snapshot at most every ``SAVE_INTERVAL`` seconds (always when forced)."""
        if force or (batch.dirty and time.monotonic() - batch.saved_at >= SAVE_INTERVAL):
            batch.snapshot.updated_at = datetime.now(UTC)
            self.repository.save_snapshot(batch.instance_id, batch.snapshot)
            batch.dirty, batch.saved_at = False, time.monotonic()

    def _candidates(self, engine, batch: Batch, preferred: list[str],
                    signals: dict) -> list[SearchResult]:
        """Search results for this goal: reused while fresh and still unread,
        so frequent live batches work through the queue instead of re-searching."""
        key = hashlib.sha256(json.dumps([request_key(batch.plan.normalized_goal), sorted(preferred)])
                             .encode()).hexdigest()[:24]
        resolved = self._resolve_sites(batch, engine)
        cached = self.repository.get_search_cache(batch.instance_id, key, settings.search_cache_minutes)
        if cached:
            results = [SearchResult(**item) for item in cached["results"]]
            unread = sum(1 for item in results if not read_recently(item.url, signals, settings.revisit_hours))
            if unread:
                note = f"Reusing search results from {cached['created_at'][11:16]} UTC ({unread} not read yet)"
                batch.notes.append(note)
                self.repository.update_run(batch.run_id, detail=note)
                return self._vet(batch, results)
        shared = self.repository.get_query_cache("search", key, settings.search_cache_minutes)
        if shared:
            results = [SearchResult(**item) for item in shared]
            batch.notes.append("search results shared with an identical earlier request")
            self.repository.set_search_cache(batch.instance_id, key, shared, "shared")
            return self._vet(batch, results)
        budget = settings.batch_seconds * DISCOVERY_SHARE
        results = discover(
            engine, batch.plan, settings.max_candidates, preferred,
            on_query=lambda query: self.repository.update_run(batch.run_id, detail=f"Searching: {query}"),
            deadline=min(batch.deadline, time.monotonic() + budget), report=batch.search_report,
            resolved=resolved,
        )
        if results:
            found = [asdict(item) for item in results]
            self.repository.set_search_cache(batch.instance_id, key, found, summarize_search(batch.search_report))
            self.repository.put_query_cache("search", key, found)
            return self._vet(batch, results)
        return self._without_search(batch, key, preferred, signals)

    def _vet(self, batch: Batch, results: list[SearchResult]) -> list[SearchResult]:
        """One request per site before any is read; sites that cannot be reached are blacklisted for everyone."""
        self.repository.update_run(batch.run_id, phase="discovery", detail=f"Checking {len(results)} sites answer")
        kept = source_health.vet(self.repository, results, batch.notes)
        if len(kept) < len(results):
            self.repository.update_run(batch.run_id, detail=f"{len(results) - len(kept)} unreachable sites set aside")
        return kept

    def _resolve_sites(self, batch: Batch, engine) -> list[str]:
        """Official sites for a request that names none (a model suggests, the program verifies)."""
        plan = batch.plan
        if plan.registry_sources or not usable_models() or settings.resolver_sites <= 0:
            return []
        if "://" in plan.normalized_goal or goal_domains(plan.normalized_goal):
            return []                                       # the request already names its site
        remembered = self.repository.get_query_cache("resolve", request_key(plan.normalized_goal),
                                                     settings.query_cache_minutes)
        if remembered:
            batch.official = {entry.split("/")[0] for entry in remembered}
            batch.notes.append("official sites (shared answer): " + ", ".join(remembered))
            return remembered
        try:
            hosts = resolve_sites(plan.normalized_goal, settings.resolver_sites, find=lambda query: [
                item.url for item in search_for(engine, query, deadline=batch.deadline, report=batch.search_report)])
        except Exception as exc:  # noqa: BLE001 - search still works without it
            logger.warning("Site resolver failed: %s", type(exc).__name__)
            return []
        if hosts:
            self.repository.put_query_cache("resolve", request_key(plan.normalized_goal), hosts)
            batch.official = {entry.split("/")[0] for entry in hosts}
            batch.notes.append("official sites: " + ", ".join(hosts))
        return hosts

    def _without_search(self, batch: Batch, key: str, preferred: list[str],
                        signals: dict) -> list[SearchResult]:
        """Candidates when search is unavailable (blocked, rate-limited, empty).

        In order: this goal's last search results however old; pages of this
        track that gave data before; the home pages of preferred and suggested
        sites (the interactive pass then follows their matching links).
        """
        stale = self.repository.get_search_cache(batch.instance_id, key, max_age_minutes=None)
        if stale and stale["results"]:
            note = f"Search unavailable; reusing results from {stale['created_at'][:16].replace('T', ' ')} UTC"
            batch.notes.append(note)
            self.repository.update_run(batch.run_id, detail=note)
            return [SearchResult(**item) for item in stale["results"]]
        fallback: list[SearchResult] = []
        productive = sorted(((url, entry) for url, entry in signals.get("visited", {}).items() if entry["accepted"]),
                            key=lambda item: -item[1]["accepted"])
        fallback += [SearchResult(url=url, title="Gave data in an earlier batch", rank=index, origin="search")
                     for index, (url, _) in enumerate(productive[:settings.max_candidates])]
        for domain in dict.fromkeys([*preferred, *batch.plan.suggested_sources]):
            fallback.append(SearchResult(url=f"https://{domain}/", title=f"Home page of {domain}",
                                         rank=len(fallback), origin="preferred" if domain in preferred else "search"))
        if fallback:
            note = (f"Search unavailable; starting from {len(productive[:settings.max_candidates])} earlier "
                    f"page(s) and {len(fallback) - len(productive[:settings.max_candidates])} known site(s)")
            batch.notes.append(note)
            self.repository.update_run(batch.run_id, detail=note)
        return fallback

    def _preferred(self, instance_id: str) -> list[str]:
        """Track-specific preferred domains first, then the global list."""
        return list(dict.fromkeys([*self.repository.get_preferred(instance_id),
                                   *self.repository.get_preferred(self.repository.preferred_scope(instance_id))]))

    def _execute(self, instance_id: str, run_id: str, cancelled: threading.Event) -> None:
        instance = self.repository.get_instance(instance_id)
        if not instance:
            return
        if cancelled.is_set():
            self.repository.update_run(run_id, status="cancelled", phase="cancelled",
                                       finished_at=datetime.now(UTC).isoformat())
            self._cancel.pop(run_id, None)
            return
        started = datetime.now(UTC).isoformat()
        self.repository.update_run(run_id, status="running", phase="planning",
                                   detail="Understanding the request", started_at=started)
        try:
            engine_settings = self._browser_settings()
            plan = self._plan(instance)
            required = ", ".join(item.name for item in plan.required_concepts) or "any value"
            window = f" · {plan.time_scope.start[:4]}–{plan.time_scope.end[:4]}" if plan.time_scope else ""
            self.repository.update_run(run_id, detail=f"Planned: {required}{window} ({plan.planner})")
            snapshot, merged = self._start_snapshot(instance_id, plan)
            batch = Batch(instance_id, run_id, plan, snapshot, cancelled, gate=self.gate,
                          nav_seconds=engine_settings.navigation_timeout_ms / 1000 if engine_settings else 30.0,
                          raw_ids={item.id for item in snapshot.raw},
                          scored_ids={item.id for name in ("accepted", "partial", "rejected")
                                      for item in getattr(snapshot, name)})
            self._freshness(batch, instance, merged)
            self._save(batch, force=True)
            preferred = self._preferred(instance_id)
            signals = self.repository.source_signals(instance_id, settings.block_cooldown_days)
            # What every track, and other installations, learned about each site.
            signals["known"] = site_knowledge(self.repository)
            batch.check_cancelled()
            with self._batch_slot():
                # Batches run concurrently; their pages queue for the shared browsers.
                batch.start_clock(settings.batch_seconds)
                events = EventBus()
                collector = ExtractionCollector(events, field_mapper=self._field_mapper(plan),
                                                site_adapters=settings.site_adapters,
                                                block_support=settings.block_support)
                collector.begin(plan)
                collector.pause()
                try:
                    with self._browsers_for(engine_settings).engine_for(events, PAGE, cancelled) as engine:
                        self.repository.update_run(run_id, phase="discovery",
                                                   detail="Discovering candidate sources")
                        results = self._candidates(engine.with_priority(SEARCH), batch, preferred, signals)
                        snapshot.candidate_count = len(results)
                        # Files returned by search are registered, not rendered;
                        # data files among them are downloaded automatically.
                        found_files = [item for result in results
                                       if (item := found_file(result.url, result.title, None, "search"))]
                        dataset_links = [DatasetLink(item.url, item.title or item.name, item.url, "file")
                                         for item in found_files if item.extension in AUTOMATIC_FILES]
                        ranked = rank(results, plan, [*preferred, *sorted(batch.official)], signals, max_per_domain=settings.max_per_domain, max_named_pages=settings.max_sources,
                                      revisit_hours=settings.revisit_hours)
                        page_titles: dict[str, str] = {}
                        if settings.deep_lane and ranked:
                            free = available_memory_mb()
                            if free is not None and free < settings.deep_lane_min_free_mb:
                                batch.notes.append(f"interactive pass skipped: {free} MB free memory "
                                                   f"(needs {settings.deep_lane_min_free_mb})")
                            else:
                                batch.lane = self._start_deep_lane(batch, engine_settings, ranked)
                        usable = attempts = 0
                        for candidate in ranked:
                            if usable >= settings.max_sources or attempts >= 2 * settings.max_sources:
                                break
                            if batch.out_of_time():
                                batch.notes.append("Time budget reached; remaining pages queued")
                                break
                            batch.check_cancelled()
                            attempts += 1
                            status, extracted = self._process_source(
                                engine, collector, batch, candidate, usable + 1, settings.max_sources)
                            if status in {"complete", "partial"}:
                                usable += 1
                            if extracted is not None:
                                dataset_links += extracted.dataset_links
                                found_files += extracted.file_links
                                page_titles[candidate.url] = extracted.title
                            if batch.lane and status in DEEP_PRIORITY:
                                batch.lane.offer(candidate.url, DEEP_PRIORITY[status], candidate.result.origin)
                            self._apply_deep(batch)
                            self._save(batch)
                        if batch.lane:
                            batch.lane.finish()
                        reason = ("Queued for the next batch (time budget reached)" if batch.out_of_time() else
                                  f"Queued for a later batch (this batch reads {settings.max_sources} usable pages)")
                        for candidate in ranked[attempts:]:
                            snapshot.sources.append(SourceOutcome(
                                id=source_id(candidate.url), url=candidate.url, title=candidate.result.title,
                                status="skipped", domain=candidate.domain, origin=candidate.result.origin,
                                rank_score=candidate.score, rank_reasons=candidate.reasons, run_id=run_id,
                                reason=reason,
                            ))
                        self._register_files(snapshot, plan, found_files, dataset_links, page_titles)
                        self._save(batch, force=True)
                    self._process_datasets(collector, batch, dataset_links)
                    self._finish_deep_lane(batch)
                    self.repository.update_run(run_id, phase="merging",
                                               detail="Merging with earlier runs and de-duplicating"
                                               if merged else "Merging and de-duplicating the dataset")
                    # Rows from both passes (and earlier batches) are judged together.
                    if merged or batch.deep_rows or len(snapshot.raw) > settings.max_raw_observations:
                        self._rescore_merged(snapshot, collector)
                finally:
                    if batch.lane:
                        batch.lane.stop()
                    collector.close()
            merge_dataset(snapshot)
            snapshot.outcome = "completed_with_data" if snapshot.accepted else (
                "completed_partial" if snapshot.partial else
                "completed_no_data" if not snapshot.raw else "completed_noise_only")
            snapshot.scored_at = datetime.now(UTC)
            self._save(batch, force=True)
            self._finish(batch)
        except BrowserUnavailable as exc:
            logger.warning("Research run %s cannot start: %s", run_id, exc)
            self.repository.update_run(run_id, status="failed", phase="failed",
                                       detail=f"Browser not available: {exc}", error=str(exc)[:500],
                                       finished_at=datetime.now(UTC).isoformat())
            self.repository.add_message(
                instance_id, "assistant",
                f"Research could not start because the browser is not available: **{exc}**\n\n"
                "Set `VORA_BROWSER_BINARY` in `.env` to a Chrome or Chromium executable, "
                "then restart the server.")
        except InterruptedError as exc:
            self.repository.update_run(run_id, status="cancelled", phase="cancelled", detail=str(exc),
                                       finished_at=datetime.now(UTC).isoformat())
        except Exception as exc:
            logger.exception("Research run %s failed", run_id)
            self.repository.update_run(run_id, status="failed", phase="failed",
                                       detail="Research failed", error=str(exc)[:500],
                                       finished_at=datetime.now(UTC).isoformat())
        finally:
            self._cancel.pop(run_id, None)
            self.hub.publish(instance_id, {"type": "batch_complete", "run_id": run_id, "refetch": True})

    # -- interactive pass ---------------------------------------------------------

    def _start_deep_lane(self, batch: Batch, engine_settings: EngineSettings | None,
                         ranked: list[RankedCandidate]) -> DeepLane:
        plan = batch.plan
        return DeepLane(
            plan=plan,
            make_engine=lambda events: self._browsers_for(engine_settings).engine_for(events, EXPLORE, batch.cancelled),
            make_collector=lambda events: ExtractionCollector(
                events, field_mapper=self._field_mapper(plan), site_adapters=settings.site_adapters,
                block_support=settings.block_support),
            gate=batch.gate, remaining=batch.remaining, cancelled=batch.cancelled,
            exclude={candidate.url for candidate in ranked}, crawl_per_site=settings.crawl_per_site,
            window=(plan.time_scope.start, plan.time_scope.end) if plan.time_scope else None,
            places=[*plan.geography, *plan.entities], forms=settings.explore_forms,
            on_detail=lambda detail: self.repository.update_run(batch.run_id, detail=detail),
        ).start()

    def _finish_deep_lane(self, batch: Batch) -> None:
        """Let the interactive pass use the rest of the budget, then collect it."""
        lane = batch.lane
        if lane is None:
            return
        while lane.alive and batch.remaining() > 0:
            batch.check_cancelled()
            self._apply_deep(batch)
            self._save(batch)
            lane.join(0.5)
        lane.stop()
        lane.join(15)
        self._apply_deep(batch)
        batch.notes.append(f"interactive pass explored {lane.explored} page(s)"
                           + (f", {lane.crawled} linked page(s) queued" if lane.crawled else ""))

    def _apply_deep(self, batch: Batch) -> None:
        """Apply what the interactive pass finished (on the batch thread only)."""
        if batch.lane is None:
            return
        for result in batch.lane.drain():
            self._apply_deep_result(batch, result)

    def _apply_deep_result(self, batch: Batch, result: DeepResult) -> None:
        item, snapshot = result.item, batch.snapshot
        if not item.url:
            batch.notes.append(result.error or "interactive pass stopped")
            return
        host = domain_of(item.url)
        raw = accepted = partial = rejected = 0
        batches = [(page.raw, page.accepted, page.partial, page.rejected) for page in result.pages]
        batches += list(result.captured)
        for rows, found, maybe, noise in batches:
            added = self._apply_rows(batch, rows, found, maybe, noise, source_url=item.url, lane="deep")
            raw, accepted, partial, rejected = (total + count for total, count in
                                                zip((raw, accepted, partial, rejected), added))
        challenged = any(page.challenge for page in result.pages)
        batch.deep_rows += raw
        notes = result.actions[:8]
        if result.captured:
            notes.append(f"{sum(len(rows) for rows, *_ in result.captured)} rows from data the page loads")
        if item.origin == "crawl":
            status = ("failed" if result.error else "blocked" if challenged and not accepted
                      else "complete" if accepted else "partial" if partial else "empty")
            snapshot.sources = [source for source in snapshot.sources
                                if not (source.url == item.url and source.run_id == batch.run_id)]
            snapshot.sources.append(SourceOutcome(
                id=source_id(item.url), url=item.url, title=result.title, status=status, extracted=raw,
                accepted=accepted, partial=partial, rejected=rejected, elapsed_seconds=result.elapsed,
                domain=host, origin="crawl", linked_from=item.linked_from, run_id=batch.run_id,
                fetched_at=datetime.now(UTC), deep_accepted=accepted, deep_notes=notes,
                reason=result.error or f"Followed from {domain_of(item.linked_from or '')}: '{item.anchor[:60]}'",
            ))
            self.repository.record_source(batch.instance_id, batch.run_id, item.url, host, status, raw, accepted)
        else:
            outcome = next((source for source in reversed(snapshot.sources)
                            if source.url == item.url and source.run_id == batch.run_id), None)
            if outcome is not None:
                outcome.deep_accepted += accepted
                outcome.deep_notes = notes or ([result.error] if result.error else [])
                outcome.extracted += raw
                outcome.accepted += accepted
                outcome.partial += partial
                outcome.rejected += rejected
                if accepted and outcome.status in {"empty", "partial"}:
                    outcome.status = "complete"
                if accepted:
                    outcome.reason = f"{accepted} more rows after interaction ({', '.join(notes[:2]) or 'rendered states'})"
        self.repository.update_run(
            batch.run_id, rows_total=len(snapshot.accepted), rows_added=len(snapshot.accepted),
            detail=(f"[deep] {host}: failed ({result.error[:80]})" if result.error else
                    f"[deep] {host}: {len(result.pages)} views · {accepted} accepted · {partial} partial"))

    @staticmethod
    def _rescore_merged(snapshot: ResearchSnapshot, collector: ExtractionCollector) -> None:
        """Judge every stored row by the current plan, newest rows kept under the cap."""
        newest_first = sorted(snapshot.raw, key=lambda item: item.fetched_at.isoformat() if item.fetched_at else "",
                              reverse=True)
        snapshot.raw = newest_first[:settings.max_raw_observations]
        snapshot.accepted, snapshot.partial, snapshot.rejected = collector.score(snapshot.raw)

    @staticmethod
    def _register_files(snapshot: ResearchSnapshot, plan: GoalPlan, found: list[FoundFile],
                        dataset_links: list[DatasetLink], page_titles: dict[str, str]) -> None:
        """Record every file found this run as a reference, most relevant first."""
        terms = [*plan.subject_terms, *(alias for concept in plan.required_concepts if concept.kind != "time"
                                        for alias in [concept.name, *concept.aliases])]
        found = [*found, *(from_dataset_link(link) for link in dataset_links if link.kind == "chart")]
        known = {entry.id: index for index, entry in enumerate(snapshot.files)}
        fresh: dict[str, FileLink] = {}
        for item in found:
            identifier = file_id(item.url)
            if identifier in known:
                # Found again: keep its status, refresh relevance for the current plan.
                index = known[identifier]
                snapshot.files[index] = snapshot.files[index].model_copy(update={
                    "relevance": file_relevance(item, terms, page_titles.get(item.linked_from or "", ""))})
                continue
            if identifier in fresh:
                continue
            fresh[identifier] = FileLink(
                id=identifier, url=item.url, name=item.name, title=item.title, extension=item.extension,
                file_type=item.file_type, found_via=item.found_via, linked_from=item.linked_from,
                relevance=file_relevance(item, terms, page_titles.get(item.linked_from or "", "")),
                extractable=item.extractable,
                status="found" if item.extractable else "unsupported",
                reason="" if item.extractable else (
                    "Programs are never downloaded" if item.file_type == "program"
                    else f".{item.extension} files are kept as links only"),
            )
        ranked = sorted(fresh.values(), key=lambda entry: entry.relevance, reverse=True)
        snapshot.files = [*snapshot.files, *ranked[:MAX_FILES_PER_RUN]]

    @staticmethod
    def _mark_file(snapshot: ResearchSnapshot, url: str, **changes) -> None:
        for index, entry in enumerate(snapshot.files):
            if entry.url == url:
                snapshot.files[index] = entry.model_copy(update=changes)

    def _process_datasets(self, collector: ExtractionCollector, batch: Batch, links: list[DatasetLink]) -> None:
        """Download the most relevant datasets the rendered pages linked to.

        Charts often hold a page's real numbers; publishers offer them as CSV.
        Rows are limited to the plan's window (and named places) before scoring.
        """
        if not links or settings.max_linked_datasets <= 0:
            return
        plan, snapshot, instance_id, run_id = batch.plan, batch.snapshot, batch.instance_id, batch.run_id
        unique = list({link.url: link for link in links}.values())
        terms = [*plan.subject_terms, *(alias for concept in plan.required_concepts if concept.kind != "time"
                                        for alias in [concept.name, *concept.aliases])]
        window = (plan.time_scope.start, plan.time_scope.end) if plan.time_scope else None
        for link in rank_links(unique, terms, settings.max_linked_datasets):
            batch.check_cancelled()
            if time.monotonic() >= batch.deadline:
                batch.notes.append("Time budget reached before all linked datasets were downloaded")
                break
            host = domain_of(link.url)
            started = time.monotonic()
            self.repository.update_run(run_id, phase="extracting",
                                       detail=f"Downloading linked dataset: {link.title} ({host})")
            self._forget(batch, link.url)
            try:
                downloaded = download_dataset(link)
                table = parse_csv(downloaded.text, window=window, places=[*plan.geography, *plan.entities])
                raw = dataset_observations(table.rows, link, title=downloaded.title, context=downloaded.context,
                                           modified_at=downloaded.modified_at, fetched_at=downloaded.fetched_at)
                self.repository.update_run(run_id, phase="scoring",
                                           detail=f"{host}: normalizing concepts and scoring {len(raw)} observations")
                accepted, partial, rejected = collector.score(raw)
                self._apply_rows(batch, raw, accepted, partial, rejected, source_url=link.url, lane="dataset")
                note = f"{table.kept:,} rows kept of {table.total:,}"
                if window and table.dropped_out_of_window:
                    note += f" ({table.dropped_out_of_window:,} outside the requested period)"
                via = "search results" if link.page_url == link.url else domain_of(link.page_url)
                snapshot.sources.append(SourceOutcome(
                    id=source_id(link.url), url=link.url, title=f"{downloaded.title} (dataset)",
                    status="complete" if accepted else "partial", extracted=len(raw),
                    accepted=len(accepted), partial=len(partial), rejected=len(rejected),
                    elapsed_seconds=round(time.monotonic() - started, 3), domain=host,
                    modified_at=downloaded.modified_at, fetched_at=downloaded.fetched_at,
                    http_status=downloaded.http_status, origin="linked_dataset", linked_from=link.page_url,
                    reason=f"Dataset from {via} · {note}", run_id=run_id,
                ))
                self._mark_file(snapshot, link.url, status="extracted", extracted_rows=len(raw),
                                accepted_rows=len(accepted), extracted_at=datetime.now(UTC),
                                size_bytes=len(downloaded.text.encode()), reason=note)
                self.repository.record_source(instance_id, run_id, link.url, host,
                                              "complete" if accepted else "partial", len(raw), len(accepted))
                self.repository.update_run(
                    run_id, detail=f"{host}: {len(raw)} observations · {len(accepted)} accepted · "
                                   f"{len(partial)} partial · {len(rejected)} noise",
                )
            except Exception as exc:
                snapshot.sources.append(SourceOutcome(
                    id=source_id(link.url), url=link.url, title=link.title, status="failed", domain=host,
                    origin="linked_dataset", linked_from=link.page_url, reason=str(exc)[:300], run_id=run_id,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                ))
                self._mark_file(snapshot, link.url, status="failed", reason=str(exc)[:300])
                self.repository.record_source(instance_id, run_id, link.url, host, "failed", 0, 0)
                self.repository.update_run(run_id, detail=f"{host}: dataset download failed ({type(exc).__name__})")
            finally:
                batch.dirty = True
                self._save(batch)
                self.repository.update_run(run_id, rows_total=len(snapshot.accepted),
                                           rows_added=len(snapshot.accepted))

    def _process_source(self, engine, collector: ExtractionCollector, batch: Batch,
                        candidate: RankedCandidate, index: int, total: int) -> tuple[str, ExtractionResult | None]:
        """Render one ranked candidate and score it. Returns its status and extraction."""
        url, host, run_id = candidate.url, candidate.domain, batch.run_id
        snapshot = batch.snapshot
        started = time.monotonic()
        self.repository.update_run(run_id, phase="rendering", detail=f"Rendering {host} ({index}/{total})")
        outcome = {"origin": candidate.result.origin, "rank_score": candidate.score,
                   "rank_reasons": candidate.reasons, "run_id": run_id}

        def on_stage(phase: str, detail: str) -> None:
            self.repository.update_run(run_id, phase=phase, detail=f"{host}: {detail}")

        status, extracted = "failed", None
        try:
            ensure_public_url(url)
            collector.begin(batch.plan, on_stage=on_stage)
            recipe_run = self._read_with_recipe(engine, collector, batch, candidate)
            if recipe_run is not None:
                status, extracted = recipe_run  # the finally block records the outcome
                return status, extracted
            result, extracted, reused = self._read_page(engine, collector, batch, url)
            self._forget(batch, url, result.final_url)
            self._apply_rows(batch, extracted.raw, extracted.accepted, extracted.partial, extracted.rejected,
                             source_url=url)
            first = extracted.raw[0] if extracted.raw else None
            status = ("blocked" if extracted.challenge else "complete" if extracted.accepted
                      else "partial" if extracted.partial else "empty")
            reason = ""
            if extracted.challenge:
                reason = "Security verification page blocked the content"
            elif status == "empty":
                noise = len(extracted.rejected)
                reason = "No data rows found" + (f" ({noise} filtered as page noise)" if noise else "")
            if reused is not None:
                reason = (f"Reused a shared read from {age_text(reused.age_seconds)}; the site was not contacted again"
                          + (f" · {reason}" if reason else ""))
            snapshot.sources.append(SourceOutcome(
                id=source_id(url), url=url, title=result.title, status=status,
                extracted=len(extracted.raw), accepted=len(extracted.accepted),
                partial=len(extracted.partial), rejected=len(extracted.rejected),
                elapsed_seconds=round(time.monotonic() - started, 3), domain=host,
                published_at=first.published_at if first else None,
                modified_at=first.modified_at if first else None,
                fetched_at=first.fetched_at if first else datetime.now(UTC),
                http_status=result.status, reason=reason, **outcome,
            ))
            self.repository.update_run(
                run_id, detail=f"{host}: {len(extracted.raw)} observations · "
                               f"{len(extracted.accepted)} accepted · {len(extracted.partial)} partial · "
                               f"{len(extracted.rejected)} noise",
            )
        except Exception as exc:
            self._forget(batch, url)
            snapshot.sources.append(SourceOutcome(
                id=source_id(url), url=url, status="failed", domain=host, reason=str(exc)[:300],
                elapsed_seconds=round(time.monotonic() - started, 3), **outcome,
            ))
            self.repository.update_run(run_id, detail=f"{host}: failed ({type(exc).__name__})")
        finally:
            collector.pause()
            accepted = len(extracted.accepted) if extracted else 0
            self.repository.record_source(batch.instance_id, run_id, url, host, status,
                                          len(extracted.raw) if extracted else 0, accepted)
            batch.dirty = True
            self.repository.update_run(run_id, rows_total=len(snapshot.accepted),
                                       rows_added=len(snapshot.accepted))
        return status, extracted

    def _read_with_recipe(self, engine, collector: ExtractionCollector, batch: Batch,
                          candidate: RankedCandidate) -> tuple[str, ExtractionResult] | None:
        """Read a registry source with its recipe. None when there is no recipe or it no longer fits
        the site; the page is then read the general way."""
        entry = registry_for_url(candidate.url, batch.plan.registry_sources)
        if entry is None and batch.recipes_done:
            # The official source the request named has answered; other sites are not what was asked.
            names = ", ".join(sorted(batch.recipes_done))
            batch.snapshot.sources.append(SourceOutcome(
                id=source_id(candidate.url), url=candidate.url, status="skipped", domain=candidate.domain,
                origin=candidate.result.origin, run_id=batch.run_id, rank_score=candidate.score,
                rank_reasons=candidate.reasons,
                reason=f"Not read: the official source you named ({names}) answered this request"))
            return "skipped", ExtractionResult()
        if entry is None and batch.learned_done and candidate.result.origin == "search"                 and not self._official(batch, candidate):
            batch.snapshot.sources.append(SourceOutcome(
                id=source_id(candidate.url), url=candidate.url, status="skipped", domain=candidate.domain,
                origin="search", run_id=batch.run_id, rank_score=candidate.score, rank_reasons=candidate.reasons,
                reason=f"Not read: the official site {', '.join(sorted(batch.learned_done))} already gave the records"))
            return "skipped", ExtractionResult()
        if entry is not None and entry.recipe is not None:
            return self._read_section(engine, collector, batch, candidate, candidate.url, entry.name, entry.recipe,
                                      entry.id, False)
        if entry is None and (candidate.result.origin == "resolved" or self._official(batch, candidate)):
            sections = self._learned_sections(engine, batch, candidate)
            if not sections:
                return None
            done = [self._read_section(engine, collector, batch, candidate, section_url, learn_key(section_url),
                                       section_recipe, learn_key(section_url), True)
                    for section_url, section_recipe in sections]
            done = [item for item in done if item is not None]
            if not done:
                return None
            merged = ExtractionResult(raw=[row for _, ex in done for row in ex.raw],
                                      accepted=[row for _, ex in done for row in ex.accepted],
                                      partial=[row for _, ex in done for row in ex.partial],
                                      rejected=[row for _, ex in done for row in ex.rejected],
                                      title=done[0][1].title)
            order = ["complete", "partial", "empty", "skipped"]
            return min((status for status, _ in done), key=order.index), merged
        return None

    @staticmethod
    def _official(batch: Batch, candidate: RankedCandidate) -> bool:
        """Whether the page is on a site the resolver named (a search result on an official site is a section of it)."""
        host = candidate.domain.removeprefix("www.")
        return any(host == site or host.endswith("." + site) for site in batch.official)

    def _read_section(self, engine, collector: ExtractionCollector, batch: Batch, candidate: RankedCandidate,
                      url: str, name: str, recipe: Recipe, key: str,
                      learned: bool) -> tuple[str, ExtractionResult] | None:
        """Read one listing with its recipe and record the outcome."""
        host, run_id, snapshot = candidate.domain, batch.run_id, batch.snapshot
        if (key in batch.learned_keys) if learned else (key in batch.recipes_done):
            # Its listing was read this batch; other pages of the site (about, help) add nothing.
            snapshot.sources.append(SourceOutcome(
                id=source_id(url), url=url, status="skipped", domain=host, origin=candidate.result.origin,
                reason=f"{name} was already read with its recipe in this batch", run_id=run_id,
                rank_score=candidate.score, rank_reasons=candidate.reasons))
            return "skipped", ExtractionResult()
        started = time.monotonic()
        id_field = recipe.id_field
        known = {str(item.fields.get(id_field)) for item in [*snapshot.accepted, *snapshot.partial]
                 if item.fields.get(id_field)}
        self.repository.update_run(run_id, detail=f"Reading {name} with its {'learned ' if learned else ''}recipe")
        shared_key = recipe_key(key, recipe)
        reused = self._shared(batch, shared_key, batch.max_age_listing)
        try:
            if reused is not None:
                run = RecipeRun(ok=True, rows=list(reused.rows), pages=0, title=reused.title,
                                final_url=reused.final_url)
            else:
                run = self._read_shared(batch, shared_key, lambda: self._run_recipe(
                    engine, batch, url, recipe, name, known), lambda read: self._keep_recipe(shared_key, url, read))
                if isinstance(run, CachedRead):            # another batch read it while we waited
                    reused, run = run, RecipeRun(ok=True, rows=list(run.rows), pages=0, title=run.title,
                                                 final_url=run.final_url)
        except Exception as exc:
            logger.warning("Recipe for %s failed: %s", key, exc)
            if learned:
                self.repository.fail_learned(key, settings.learned_max_failures)
            batch.notes.append(f"{name}: recipe failed ({type(exc).__name__}); read the general way")
            return None
        if not run.ok:
            if learned:
                self.repository.fail_learned(key, settings.learned_max_failures)
            batch.notes.append(f"{name}: the site's layout changed ({run.note}); read the general way")
            self.repository.update_run(run_id, detail=f"{host}: recipe did not fit ({run.note})")
            return None
        accepted, partial, rejected = collector.score(run.rows)
        if learned:
            if accepted or partial:
                self.repository.touch_learned(key)
                batch.learned_done.add(host)
                batch.learned_keys.add(key)
            else:
                self.repository.fail_learned(key, settings.learned_max_failures)   # records the request cannot use
        elif run.rows:
            batch.recipes_done.add(key)
        extracted = ExtractionResult(raw=list(run.rows), accepted=accepted, partial=partial, rejected=rejected,
                                     title=run.title)
        self._apply_rows(batch, extracted.raw, accepted, partial, rejected, source_url=url)
        status = "complete" if accepted else "partial" if partial else "empty"
        new = sum(1 for row in run.rows if str(row.fields.get(id_field)) not in known)
        reason = (f"Read with the {name} {'learned ' if learned else ''}recipe: {run.pages} page(s), {len(run.rows)} records, {new} new"
                  + (" (stopped at records already collected)" if run.stopped_at_known else ""))
        if reused is not None:
            reason = (f"Reused a shared read of the {name} listing from {age_text(reused.age_seconds)}: "
                      f"{len(run.rows)} records, {new} new; the site was not contacted again")
        snapshot.sources.append(SourceOutcome(
            id=source_id(url), url=url, title=run.title, status=status, extracted=len(run.rows),
            accepted=len(accepted), partial=len(partial), rejected=len(rejected),
            elapsed_seconds=round(time.monotonic() - started, 3), domain=host, fetched_at=datetime.now(UTC),
            reason=reason, origin=candidate.result.origin, rank_score=candidate.score,
            rank_reasons=candidate.reasons, run_id=run_id,
        ))
        self.repository.update_run(run_id, detail=f"{host}: {reason}")
        return status, extracted

    # -- shared reads of public sources --------------------------------------------------------------------------

    def _freshness(self, batch: Batch, instance: dict, continuing: bool) -> None:
        """How old a shared read may be for this batch. A forced run accepts none; a live track's repeat batch only
        reads newer than its own cadence (so tracking still sees new rows); a first run accepts the full lifetime."""
        fresh = batch.run_id in self._fresh_runs
        self._fresh_runs.discard(batch.run_id)
        if not settings.source_cache or fresh:
            return
        listing, page = settings.source_ttl_listing_minutes * 60, settings.source_ttl_page_minutes * 60
        if instance.get("live_enabled") and continuing:
            listing = page = min(listing, page, settings.live_interval_seconds)
        batch.max_age_listing, batch.max_age_page = listing, page

    def _shared(self, batch: Batch, key: str, max_age: float) -> CachedRead | None:
        if not settings.source_cache or max_age <= 0:
            return None
        found = self.sources.lookup(key, max_age)
        if found is not None:
            batch.reused += 1
        return found

    def _read_shared(self, batch: Batch, key: str, read, keep):
        """Read a source once even when several batches need it at the same moment: the first reads it, the others
        wait and take its result (a ``CachedRead``). ``keep(result)`` stores what was read."""
        if not settings.source_cache:
            return read()
        waiting = self.sources.claim(key)
        if waiting is not None:
            self.repository.update_run(batch.run_id, detail="Another request is reading this source; joining it")
            found = self.sources.wait_for(key, batch.max_age_page, batch.deadline)
            if found is not None:
                batch.reused += 1
                return found
            waiting = self.sources.claim(key)
            if waiting is not None:                     # still busy and our wait ran out: read it ourselves
                return read()
        try:
            result = read()
            try:
                keep(result)
            except Exception:  # noqa: BLE001 - sharing is an optimisation; the run has its rows anyway
                logger.exception("Could not store a shared read of %s", key)
            return result
        finally:
            self.sources.release(key)

    def _run_recipe(self, engine, batch: Batch, url: str, recipe: Recipe, name: str, known: set[str]) -> RecipeRun:
        batch.gate.wait(url)
        return engine.run_recipe(url, recipe, source_name=name, known_ids=known,
                                 max_pages=settings.api_max_pages if recipe.kind == "json_api" else settings.recipe_max_pages,
                                 deadline=batch.deadline)

    def _keep_recipe(self, key: str, url: str, run: RecipeRun) -> None:
        # A read that stopped at records this track already had is only part of the listing: it may only add to a
        # complete read stored earlier, never stand in for one.
        if not run.ok or not run.rows:
            return
        if run.stopped_at_known and not self.sources.has(key):
            return
        self.sources.store(key, url=url, final_url=run.final_url or url, title=run.title, status=200,
                           challenge=False, rows=run.rows, merge=run.stopped_at_known)

    def _read_page(self, engine, collector: ExtractionCollector, batch: Batch, url: str):
        """Render a page, or reuse a fresh shared read of it. Returns (result, extraction, the shared read or None)."""
        key = page_key(url)
        found = self._shared(batch, key, batch.max_age_page)

        def read():
            batch.gate.wait(url)
            result = engine.execute(url)
            return result, collector.take(result.execution_id)

        def keep(pair) -> None:
            result, extracted = pair
            # Pages that failed or showed a verification screen are not worth sharing.
            if extracted.challenge or (result.status or 0) >= 400:
                return
            self.sources.store(key, url=url, final_url=result.final_url, title=result.title, status=result.status or 0,
                               challenge=False, rows=extracted.raw, dataset_links=extracted.dataset_links,
                               file_links=extracted.file_links)

        if found is None:
            outcome = self._read_shared(batch, key, read, keep)
            if not isinstance(outcome, CachedRead):
                result, extracted = outcome
                return result, extracted, None
            found = outcome
        # A shared read: this track scores the shared rows with its own plan.
        accepted, partial, rejected = collector.score(found.rows)
        extracted = ExtractionResult(
            raw=list(found.rows), accepted=accepted, partial=partial, rejected=rejected, title=found.title,
            challenge=found.challenge,
            dataset_links=[DatasetLink(**item) for item in found.dataset_links],
            file_links=[FoundFile(**item) for item in found.file_links])
        result = SimpleNamespace(final_url=found.final_url, title=found.title, status=found.status)
        return result, extracted, found

    def _learned_sections(self, engine, batch: Batch, candidate: RankedCandidate) -> list[tuple[str, Recipe]]:
        """The listings of a resolved site that the request is about, ``[(address, recipe)]``: kept from an earlier
        learning, or learned now (the learner opens the page, reads its listing or its data service, and follows
        links the request points at; see ``vora.learning.structure``)."""
        host, url = candidate.domain, candidate.url
        key = learn_key(url)
        repository = self.repository
        stored = repository.get_learned(key, settings.learned_ttl_days)
        if stored is not None:
            recipe = stored["recipe"]
            if recipe is None:
                return []                                    # looked at recently: nothing readable there
            addresses = recipe["urls"] if recipe.get("kind") == "sections" else [url]
            found = []
            for address in addresses:
                part = stored if address == url else repository.get_learned(learn_key(address), settings.learned_ttl_days)
                try:
                    found.append((address, Recipe.model_validate(part["recipe"])))
                except (TypeError, ValueError, KeyError):
                    repository.fail_learned(learn_key(address), 1)
            return found
        remaining = batch.deadline - time.monotonic() if math.isfinite(batch.deadline) else 240
        if remaining < 40:
            return []
        repository.update_run(batch.run_id, phase="rendering", detail=f"Learning how {host} lists its records")
        try:
            learned = engine.learn_structure(url, names_fn=name_columns, budget=min(240, remaining - 10),
                                             goal=batch.plan.normalized_goal)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Learning %s failed: %s", host, exc)
            return []
        if not learned.ok or not learned.recipe:
            repository.save_learned(key, url, None)
            batch.notes.append(f"{host}: no readable listing found ({'; '.join(learned.notes[-1:])})")
            return []
        if learned.sections:
            for section in learned.sections:
                repository.save_learned(learn_key(section["url"]), section["url"], section["recipe"])
            repository.save_learned(key, url, {"kind": "sections", "urls": [s["url"] for s in learned.sections]})
            batch.notes.append(f"{host}: learned {len(learned.sections)} listings through its links")
            return [(s["url"], Recipe.model_validate(s["recipe"])) for s in learned.sections]
        repository.save_learned(key, url, learned.recipe)
        batch.notes.append(f"{host}: learned its listing ({learned.recipe.get('kind')})")
        return [(url, Recipe.model_validate(learned.recipe))]

    def _diagnostics(self, batch: Batch, attempted: list[SourceOutcome]) -> str:
        """One line that explains a batch: planner, search, page outcomes, time."""
        parts = [f"planner {batch.plan.planner}"]
        if batch.search_report:
            parts.append(f"search {summarize_search(batch.search_report)}")
        statuses = Counter(source.status for source in attempted)
        if statuses:
            parts.append("pages " + ", ".join(f"{count} {status}" for status, count in statuses.most_common()))
        deep = sum(source.deep_accepted for source in attempted)
        if deep:
            parts.append(f"{deep} rows from the interactive pass")
        if math.isfinite(batch.deadline):
            parts.append(f"{time.monotonic() - batch.started:.0f}s of {settings.batch_seconds}s budget")
        if batch.reused:
            parts.append(f"{batch.reused} source(s) reused from a shared read")
        parts += batch.notes
        return " · ".join(parts)

    def _finish(self, batch: Batch) -> None:
        snapshot, run_id = batch.snapshot, batch.run_id
        attempted = [source for source in snapshot.sources
                     if source.run_id == run_id and source.status != "skipped"]
        no_candidates = not attempted and not snapshot.candidate_count
        all_failed = no_candidates or (
            bool(attempted) and all(source.status in {"failed", "blocked"} for source in attempted))
        diagnostics = self._diagnostics(batch, attempted)
        summary = (f"{len(snapshot.raw)} observations fetched · {len(snapshot.accepted)} accepted · "
                   f"{len(snapshot.partial)} partial · {len(snapshot.rejected)} filtered as noise")
        if no_candidates:
            detail = "Search returned no results" + (
                f" ({summarize_search(batch.search_report)})" if batch.search_report else "")
        elif all_failed:
            detail = "Every selected source failed or was blocked before content could be received"
        else:
            detail = summary
        self.repository.update_run(run_id, detail=f"Diagnostics: {diagnostics}")
        self.repository.update_run(
            run_id, status="failed" if all_failed else "succeeded",
            phase="failed" if all_failed else "complete", detail=detail,
            error=detail if all_failed else None,
            rows_total=len(snapshot.accepted), rows_added=len(snapshot.accepted),
            finished_at=datetime.now(UTC).isoformat(),
        )
        if no_candidates:
            message = (f"{detail}. The search engines may be blocking automated searches from this "
                       "network; try again later or add preferred sources.")
        elif all_failed:
            message = ("Research could not receive usable content from any selected source. "
                       "Open **Sources** for the recorded failure reasons.")
        else:
            plan = snapshot.plan
            window = f" for **{plan.time_scope.start[:4]}–{plan.time_scope.end[:4]}**" if plan.time_scope else ""
            message = (f"Research complete{window}: **{len(snapshot.accepted)} accepted** observations, "
                       f"{len(snapshot.partial)} partial (kept for review), "
                       f"{len(snapshot.rejected)} filtered as page noise, from "
                       f"{len([s for s in attempted if s.status in {'complete', 'partial'}])} sources.")
        self.repository.add_message(batch.instance_id, "assistant", f"{message}\n\n_Diagnostics: {diagnostics}_")
