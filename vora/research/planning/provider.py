"""Small, bounded LLM abstraction with deterministic planning authority.

Models contribute vocabulary: synonyms for the requested measure, subject
aliases ("EV" -> "electric vehicle"), optional enrichment dimensions and
search queries. They never decide what is mandatory and never compute dates;
``vora.extraction.requirements`` grounds every suggestion in the user's own words
and resolves time windows deterministically.
"""

from __future__ import annotations

import logging
import re
import os
import time
from datetime import date
from threading import Lock, Thread
from typing import Literal

from pydantic import BaseModel, Field

from vora.settings import settings
from vora.extraction.requirements import DraftConcept, PlannerDraft, align_query_years, build_requirements, default_queries
from vora.extraction.temporal import today_utc
from vora.shared.contracts import GoalPlan
from vora.shared.cache import MISSING, BoundedCache

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
logger = logging.getLogger("vora.llm")


class LLMUnavailable(RuntimeError):
    pass


class _DraftMeasure(BaseModel):
    name: str = Field(description="Canonical measure the user asked for, e.g. 'price', 'yield'")
    aliases: list[str] = Field(default_factory=list, max_length=12,
                               description="Column names or phrases sources use for this measure")


class _LLMDraft(BaseModel):
    intent: Literal["dataset", "factual", "explanation"] = "dataset"
    subject_terms: list[str] = Field(default_factory=list, max_length=10,
                                     description="Topic words and their synonyms/acronym expansions")
    measures: list[_DraftMeasure] = Field(default_factory=list, max_length=4)
    optional_dimensions: list[str] = Field(default_factory=list, max_length=10)
    geography: list[str] = Field(default_factory=list, max_length=6)
    entities: list[str] = Field(default_factory=list, max_length=6)
    search_queries: list[str] = Field(default_factory=list, max_length=4)
    authoritative_sources: list[str] = Field(default_factory=list, max_length=6,
                                             description="Domains of well-known publishers of this data")


class _FieldPair(BaseModel):
    header: str
    concept: str | None = None


class _FieldMapping(BaseModel):
    mappings: list[_FieldPair] = Field(default_factory=list)


_PLAN_PROMPT = """You help plan a web research dataset. Read the goal and return JSON.

Rules:
- measures: only the quantities the user explicitly asked about (e.g. "price",
  "yield", "revenue"). Give each a canonical name and aliases that web tables
  commonly use for it (e.g. price: msrp, cost, average price, sale price).
- subject_terms: the topic words from the goal plus synonyms and acronym
  expansions (e.g. "EV" -> "electric vehicle", "electric car"). Do not include
  measures or time words.
- optional_dimensions: columns that would enrich rows but are NOT required
  (e.g. make, model, country, crop, currency). Never invent mandatory columns.
- geography / entities: only constraints stated in the goal.
- search_queries: up to 4 web search queries. Copy any time expression from the
  goal verbatim; do not convert relative periods into specific years.
- authoritative_sources: up to 6 bare domains (e.g. "fao.org") of well-known
  publishers of this kind of data for the region in the goal. Only real sites.

Goal: {goal!r}"""

_MAPPING_PROMPT = """Map web table headers to requested dataset concepts.
Goal: {goal!r}
Concepts: {concepts}
Headers: {headers}
For each header return the single concept it measures, or null if none."""


def _models() -> list[str]:
    result = [settings.llm_model]
    if settings.llm_fallback_model and settings.llm_fallback_model not in result:
        result.append(settings.llm_fallback_model)
    return result


def _credentials(model: str) -> dict[str, str]:
    if model.startswith("openai/nvidia/"):
        if not settings.nvidia_api_key:
            raise LLMUnavailable("NVIDIA_API_KEY is not configured")
        return {"api_key": settings.nvidia_api_key, "api_base": settings.nvidia_api_base}
    if model.startswith("gemini/"):
        if not settings.gemini_api_key:
            raise LLMUnavailable("GEMINI_API_KEY is not configured")
        return {"api_key": settings.gemini_api_key}
    return {}


def configured_models() -> list[str]:
    available = []
    for model in _models():
        try:
            _credentials(model)
            available.append(model)
        except LLMUnavailable:
            continue
    return available


# A model that just failed (timeout, outage, quota) is skipped for a while so
# every later call goes straight to the fallback instead of waiting again.
_cooldown_until: dict[str, float] = {}
_cooldown_lock = Lock()


def _cooling(model: str) -> bool:
    with _cooldown_lock:
        return _cooldown_until.get(model, 0.0) > time.monotonic()


def _cool_down(model: str) -> None:
    with _cooldown_lock:
        _cooldown_until[model] = time.monotonic() + settings.llm_cooldown_seconds


def available_models() -> list[str]:
    """Configured models not currently cooling down after a failure."""
    return [model for model in configured_models() if not _cooling(model)]


def _structured(prompt: str, response_model: type[BaseModel], timeout: int) -> tuple[BaseModel, str]:
    try:
        import instructor
        import litellm
        from litellm import completion
    except Exception as exc:  # pragma: no cover - optional dependency failure
        raise LLMUnavailable(str(exc)) from exc
    litellm.suppress_debug_info = True  # no "Give Feedback / Get Help" banner per failure
    client = instructor.from_litellm(completion)
    errors: list[str] = []
    candidates = available_models()
    if not candidates and configured_models():
        raise LLMUnavailable("All configured models are cooling down after recent failures")
    for model in candidates:
        try:
            response = client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": prompt}],
                response_model=response_model, max_retries=1, timeout=timeout,
                **_credentials(model),
            )
            return response, model
        except Exception as exc:
            errors.append(f"{model}: {type(exc).__name__}")
            _cool_down(model)
            logger.warning("LLM call failed on %s (%s); skipping it for %ss",
                           model, type(exc).__name__, settings.llm_cooldown_seconds)
    raise LLMUnavailable("; ".join(errors) or "No language model is configured")


def _draft(goal: str) -> tuple[PlannerDraft, str] | None:
    try:
        response, model = _structured(_PLAN_PROMPT.format(goal=goal), _LLMDraft, settings.llm_timeout_seconds)
    except LLMUnavailable:
        return None
    assert isinstance(response, _LLMDraft)
    return PlannerDraft(
        intent=response.intent,
        subject_terms=response.subject_terms,
        measures=[DraftConcept(item.name, list(item.aliases)) for item in response.measures],
        dimensions=response.optional_dimensions,
        geography=response.geography, entities=response.entities,
        search_queries=response.search_queries,
        suggested_sources=response.authoritative_sources,
    ), model


def _draft_within(goal: str, seconds: float) -> tuple[tuple[PlannerDraft, str] | None, bool]:
    """The model's draft if it arrives within ``seconds``; (None, True) if too slow.

    The call runs on a daemon thread: a slow provider is abandoned (its answer
    ignored) instead of holding up the batch or the server's shutdown.
    """
    box: dict[str, tuple[PlannerDraft, str] | None] = {}

    def work() -> None:
        try:
            box["draft"] = _draft(goal)
        except Exception:  # a provider error means "no draft"
            logger.warning("Planning with the language model failed", exc_info=True)
            box["draft"] = None

    thread = Thread(target=work, name="vora-plan", daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        return None, True
    return box.get("draft"), False


# Queries for a shortlist ("best 10 …", "top 5", "cheapest"): they miss most of a full list.
SHORTLIST = re.compile(r"\b(?:best|top\s*\d*|cheapest|most\s+popular|leading|favou?rite)\b", re.I)


def apply_registry(goal: str, fields: dict) -> None:
    """Official sources the request names (data/sources.json): read first, and named in the plan."""
    from vora.learning.source_registry import match

    entries = match(goal, fields.get("source_mentions"))
    if not entries:
        return
    fields["registry_sources"] = [entry.id for entry in entries][:6]
    names = [entry.aliases[0] if entry.aliases else entry.name for entry in entries]
    fields["source_mentions"] = list(dict.fromkeys([*names, *fields.get("source_mentions", [])]))[:6]
    domains = [domain for entry in entries for domain in entry.domains]
    fields["suggested_sources"] = list(dict.fromkeys([*domains, *fields.get("suggested_sources", [])]))[:6]
    if fields.get("answer_shape") == "either" and any(entry.record_type for entry in entries):
        fields["answer_shape"] = "records"


def analyze_goal(goal: str, *, use_llm: bool = True, today: date | None = None) -> GoalPlan:
    """Plan a research goal. The deterministic planner is always the authority.

    The language model gets at most ``VORA_LLM_PLAN_SECONDS``; a slower
    provider is skipped for this plan and the deterministic planner is used.
    """
    today = today or today_utc()
    drafted, too_slow = _draft_within(goal, settings.llm_plan_seconds) if use_llm else (None, False)
    draft, model = drafted if drafted else (None, None)
    fields = build_requirements(goal, draft=draft, today=today)
    apply_registry(goal, fields)
    corrected_goal = fields.pop("corrected_goal")
    scope = fields["time_scope"]
    queries = [align_query_years(query, scope) for query in (draft.search_queries if draft else [])]
    queries = [query for query in dict.fromkeys(" ".join(query.split()) for query in queries) if query]
    if fields["answer_shape"] != "quantities":
        # A request for a list is not answered by "best 10" or "cheapest" pages (shortlists).
        queries = [query for query in queries if not SHORTLIST.search(query) or SHORTLIST.search(goal)]
    for query in default_queries(corrected_goal, scope, fields["answer_shape"], fields["source_mentions"]):
        if len(queries) >= 4:
            break
        if query not in queries:
            queries.append(query)
    notes = list(fields.pop("notes"))
    if model:
        notes.insert(0, f"Vocabulary suggested by {model}; requirements grounded in the request.")
    elif too_slow:
        notes.insert(0, f"Language model slower than {settings.llm_plan_seconds}s; deterministic planner used.")
    elif use_llm:
        notes.insert(0, "Language model unavailable; deterministic planner used.")
    return GoalPlan(
        normalized_goal=" ".join(goal.split()),
        intent=draft.intent if draft and draft.intent in {"dataset", "factual", "explanation"} else "dataset",
        search_queries=queries[:4],
        planner=f"llm:{model}" if model else "heuristic",
        notes=notes,
        **fields,
    )


def heuristic_plan(goal: str, today: date | None = None) -> GoalPlan:
    return analyze_goal(goal, use_llm=False, today=today)


# What a table header means for a set of concepts, learned from a model.
# Key: (header, sorted concept names). Bounded: at most 20,000 entries, each kept 30 days;
# nothing else invalidates it, since a header's meaning does not change with the goal.
_mapping_cache = BoundedCache(max_entries=20_000, ttl_seconds=30 * 24 * 3600)


def map_fields(headers: list[str], concepts: list[str], goal: str) -> dict[str, str | None]:
    """Resolve headers the lexicon could not place, once per (header, concepts)."""
    key_concepts = tuple(sorted(concepts))
    known = {}
    for header in headers:
        cached = _mapping_cache.get((header, key_concepts))
        if cached is not MISSING:
            known[header] = cached
    pending = [header for header in headers if header not in known]
    if pending and settings.semantic_llm:
        try:
            response, _ = _structured(
                _MAPPING_PROMPT.format(goal=goal, concepts=list(key_concepts), headers=pending),
                _FieldMapping, min(15, settings.llm_timeout_seconds),
            )
            assert isinstance(response, _FieldMapping)
            answers = {pair.header: pair.concept if pair.concept in key_concepts else None
                       for pair in response.mappings}
        except LLMUnavailable:
            answers = {}
        for header in pending:
            _mapping_cache.set((header, key_concepts), answers.get(header))
            known[header] = answers.get(header)
    return known
