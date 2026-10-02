"""Lifecycle listener that extracts only completed engine executions."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import RLock

from vora.browser.contracts import ExecutionResult
from vora.browser.events import EventBus, LifecycleEvent, RuntimeEvent
from vora.shared.contracts import GoalPlan, Observation

from vora.extraction.datasets import DatasetLink
from vora.extraction.files import FoundFile
from vora.extraction.parser import parse_page
from vora.extraction.scoring import FieldMapper, ObservationScorer

logger = logging.getLogger("vora.extraction")

StageCallback = Callable[[str, str], None]


@dataclass(slots=True)
class ExtractionResult:
    raw: list[Observation] = field(default_factory=list)
    accepted: list[Observation] = field(default_factory=list)
    partial: list[Observation] = field(default_factory=list)
    rejected: list[Observation] = field(default_factory=list)
    title: str = ""
    challenge: bool = False
    dataset_links: list[DatasetLink] = field(default_factory=list)
    file_links: list[FoundFile] = field(default_factory=list)


class ExtractionCollector:
    """Parse and score pages as the engine completes them.

    One scorer is kept per plan so semantic mappings (and any model-resolved
    headers) are reused across every page of a run.
    """

    def __init__(self, events: EventBus, field_mapper: FieldMapper | None = None,
                 site_adapters: bool = True, block_support: bool = True) -> None:
        self._site_adapters = site_adapters
        self._block_support = block_support
        self._lock = RLock()
        self._plan: GoalPlan | None = None
        self._scorer: ObservationScorer | None = None
        self._on_stage: StageCallback | None = None
        self._field_mapper = field_mapper
        self._results: dict[str, ExtractionResult] = {}
        self._unsubscribe = events.subscribe(LifecycleEvent.EXECUTION_COMPLETE, self._on_complete)

    def begin(self, plan: GoalPlan, on_stage: StageCallback | None = None) -> None:
        with self._lock:
            if self._plan is not plan or self._scorer is None:
                self._scorer = ObservationScorer(plan, block_support=self._block_support)
            self._plan = plan
            self._on_stage = on_stage

    def pause(self) -> None:
        with self._lock:
            self._plan = None
            self._on_stage = None

    def take(self, execution_id: str) -> ExtractionResult:
        with self._lock:
            return self._results.pop(execution_id, ExtractionResult())

    def close(self) -> None:
        self._unsubscribe()

    def score(self, observations: list[Observation]) -> tuple[list[Observation], list[Observation], list[Observation]]:
        """Score observations obtained outside a page render (e.g. linked datasets)."""
        with self._lock:
            scorer = self._scorer
        if scorer is None:
            raise RuntimeError("Call begin(plan) before scoring")
        return scorer.score_all(observations)

    def _stage(self, callback: StageCallback | None, phase: str, detail: str) -> None:
        if callback is None:
            return
        try:
            callback(phase, detail)
        except Exception:
            logger.exception("Stage callback failed")

    def _on_complete(self, event: RuntimeEvent) -> None:
        result = event.data.get("result")
        with self._lock:
            plan, scorer, on_stage = self._plan, self._scorer, self._on_stage
        if plan is None or scorer is None or not isinstance(result, ExecutionResult):
            return
        self._stage(on_stage, "extracting", "Extracting observations from the rendered page")
        page = parse_page(result, site_adapters=self._site_adapters)
        raw = page.observations
        self._stage(on_stage, "scoring", f"Normalizing concepts and scoring {len(raw)} observations")
        accepted, partial, rejected = scorer.score_all(raw)
        if self._field_mapper and partial:
            try:
                if scorer.learn_from(partial, self._field_mapper):
                    retry = [row for row in raw if row.id in {item.id for item in partial}]
                    rescored_accepted, partial, more_rejected = scorer.score_all(retry)
                    accepted += rescored_accepted
                    rejected += more_rejected
            except Exception:
                logger.exception("Semantic field mapping failed; keeping deterministic scores")
        with self._lock:
            self._results[event.execution_id] = ExtractionResult(
                raw=raw, accepted=accepted, partial=partial, rejected=rejected, title=result.title,
                challenge=any(row.content_role == "challenge" for row in raw),
                dataset_links=page.dataset_links,
                file_links=page.file_links,
            )
