"""Application DTOs with no dependency on browser or storage implementations."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ConceptRequirement(BaseModel):
    """A semantic concept the request needs, not a literal column name."""

    name: str
    role: Literal["required", "optional"] = "required"
    kind: Literal["measure", "time", "dimension", "entity", "topic"] = "measure"
    aliases: list[str] = Field(default_factory=list, max_length=24)
    source: Literal["goal", "planner", "ontology", "legacy"] = "goal"


class TimeScope(BaseModel):
    """A deterministically resolved request-level time constraint."""

    expression: str
    start: str
    end: str
    granularity: str
    relative: bool = False
    policy: str = ""
    periods: list[str] = Field(default_factory=list)
    resolved_on: str | None = None


class GoalPlan(BaseModel):
    normalized_goal: str
    intent: Literal["dataset", "factual", "explanation"] = "dataset"
    search_queries: list[str] = Field(default_factory=list, max_length=8)
    # Legacy view of the required concepts, kept for API compatibility.
    required_fields: list[str] = Field(default_factory=list, max_length=20)
    year_start: int | None = None
    year_end: int | None = None
    subject_terms: list[str] = Field(default_factory=list, max_length=24)
    # Goal nouns that name the counted thing ("hospitals", "wins"); a numeric
    # column named after one counts as the requested quantity.
    subject_heads: list[str] = Field(default_factory=list, max_length=12)
    concepts: list[ConceptRequirement] = Field(default_factory=list, max_length=24)
    time_scope: TimeScope | None = None
    geography: list[str] = Field(default_factory=list, max_length=12)
    entities: list[str] = Field(default_factory=list, max_length=12)
    # What the request wants back. "quantities": numbers of a named measure (the
    # original behaviour, kept for stored plans). "records": entries such as
    # documents, events or companies, where no number is needed. "either": the
    # request names no measure, so a row with a number about the subject and a
    # well-formed record about it are both answers.
    answer_shape: Literal["quantities", "records", "either"] = "quantities"
    # Names in the request that point at a publisher or portal ("from example",
    # "RBI circulars"). They are looked up ("<name> official website") and a site
    # whose own name matches one is ranked as that source.
    source_mentions: list[str] = Field(default_factory=list, max_length=6)
    # Registry entries (data/sources.json) the request names; they are read first, with their recipe.
    registry_sources: list[str] = Field(default_factory=list, max_length=6)
    planner: str = "heuristic"
    notes: list[str] = Field(default_factory=list, max_length=12)
    # Domains a model suggested as well-known publishers for this data. They
    # only boost ranking; their rows are scored like any other.
    suggested_sources: list[str] = Field(default_factory=list, max_length=6)

    @property
    def required_concepts(self) -> list[ConceptRequirement]:
        return [item for item in self.concepts if item.role == "required"]

    @property
    def optional_concepts(self) -> list[ConceptRequirement]:
        return [item for item in self.concepts if item.role == "optional"]


class CreateInstanceRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(default="", max_length=4000)


class DomainList(BaseModel):
    domains: list[str] = Field(default_factory=list, max_length=100)


class UpdateInstanceRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None
    live_enabled: bool | None = None


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


class InstanceOut(BaseModel):
    id: str
    title: str
    goal: str
    archived: bool
    live_enabled: bool
    created_at: datetime
    updated_at: datetime
    dataset_row_count: int = 0


class MessageOut(BaseModel):
    id: str
    instance_id: str
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: datetime


class RunOut(BaseModel):
    id: str
    instance_id: str
    status: RunStatus
    phase: str
    detail: str = ""
    rows_total: int = 0
    rows_added: int = 0
    error: str | None = None
    cancellation_requested: bool = False
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime


Tier = Literal["high", "usable", "partial", "low", "noise", "unscored"]


class Observation(BaseModel):
    id: str = ""
    source_url: str
    method: str
    fields: dict[str, str]
    status: Literal["raw", "partial", "accepted", "rejected"] = "raw"
    reasons: list[str] = Field(default_factory=list)
    content_role: Literal["data", "metadata", "navigation", "challenge", "noise"] = "data"

    # Provenance: where and when the observation was obtained. Never used as
    # observation values.
    source_domain: str = ""
    source_title: str = ""
    published_at: str | None = None
    modified_at: str | None = None
    # Set by the parser; None for observations stored before fetch times were recorded.
    fetched_at: datetime | None = None
    extraction_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    context: str = ""
    context_kind: str = ""
    temporal_coverage: str | None = None
    # The page block the row came from ("table#2", "tab: 2023/table#1"); rows
    # of one block are judged together (see vora.extraction.blocks).
    block_id: str = ""

    # Temporal interpretation: which period the values describe.
    data_period: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    period_granularity: str | None = None
    time_basis: str = "none"
    time_inferred: bool = False
    temporal_confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    # Semantic scoring.
    tier: Tier = "unscored"
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    concept_matches: dict[str, dict[str, Any]] = Field(default_factory=dict)
    normalized: dict[str, str] = Field(default_factory=dict)
    noise_probability: float = Field(default=0.0, ge=0.0, le=1.0)
    relevance_score: float = Field(default=0.0, ge=0.0, le=1.0)
    quality_score: float = Field(default=0.0, ge=0.0, le=1.0)
    matched_fields: list[str] = Field(default_factory=list)

    @field_validator("data_period", mode="before")
    @classmethod
    def _legacy_period(cls, value: Any) -> Any:
        # Early snapshots stored a list of years.
        if isinstance(value, list):
            years = sorted(int(item) for item in value if str(item).isdigit())
            if not years:
                return None
            return str(years[0]) if years[0] == years[-1] else f"{years[0]}–{years[-1]}"
        return value

    @model_validator(mode="after")
    def _identity(self) -> "Observation":
        from vora.shared.urls import strip_session

        if "(" in self.source_url:
            self.source_url = strip_session(self.source_url)
        # A link inside a row (a document, a detail page) must not carry the reader's session either: it would
        # expire and land on the site's error page.
        for name, value in self.fields.items():
            if name.endswith("_url") and "(" in value and value.startswith("http"):
                self.fields[name] = strip_session(value)
        if not self.id:
            from vora.extraction.records import natural_key

            key = natural_key(self.fields)
            if key:
                # The same notice, circular or filing read from two pages (or two runs) is one row.
                domain = urlparse(self.source_url).netloc.removeprefix("www.")
                payload = json.dumps([domain, key], ensure_ascii=False)
            else:
                payload = json.dumps([self.source_url, self.method, self.fields], sort_keys=True,
                                     ensure_ascii=False)
            self.id = hashlib.sha256(payload.encode()).hexdigest()[:16]
        if not self.source_domain and self.source_url.startswith("http"):
            self.source_domain = urlparse(self.source_url).netloc.removeprefix("www.")
        return self


class SourceOutcome(BaseModel):
    id: str
    url: str

    @field_validator("url", "linked_from", mode="after", check_fields=False)
    @classmethod
    def _no_session(cls, value):
        from vora.shared.urls import strip_session

        return strip_session(value) if isinstance(value, str) else value
    title: str = ""
    # empty: rendered, but no accepted or partial rows (nothing usable found).
    status: Literal["selected", "complete", "partial", "empty", "failed", "skipped", "blocked"]
    extracted: int = 0
    accepted: int = 0
    partial: int = 0
    rejected: int = 0
    elapsed_seconds: float = 0.0
    reason: str = ""
    domain: str = ""
    published_at: str | None = None
    modified_at: str | None = None
    fetched_at: datetime | None = None
    http_status: int | None = None
    # How the source was found and why it was chosen.
    origin: Literal["registry", "resolved", "goal", "preferred", "search", "linked_dataset", "file", "crawl"] = "search"
    linked_from: str | None = None
    rank_score: float | None = None
    rank_reasons: list[str] = Field(default_factory=list)
    run_id: str | None = None
    # What the interactive pass (tabs, "load more", iframes, chart data) added.
    deep_accepted: int = 0
    deep_notes: list[str] = Field(default_factory=list)


FileType = Literal["data", "spreadsheet", "document", "structured", "archive", "media", "program", "other"]


class FileLink(BaseModel):
    """A downloadable file found during research, kept as a reference.

    Small tabular files (CSV) are extracted automatically; other supported
    formats are extracted only when the user asks; programs are never fetched.
    """

    id: str
    url: str
    name: str
    title: str = ""
    extension: str = ""
    file_type: FileType = "other"
    found_via: Literal["page", "search", "chart"] = "page"
    linked_from: str | None = None
    relevance: float = Field(default=0.0, ge=0.0, le=1.0)
    extractable: bool = False
    status: Literal["found", "extracted", "failed", "unsupported"] = "found"
    reason: str = ""
    size_bytes: int | None = None
    content_type: str | None = None
    extracted_rows: int = 0
    accepted_rows: int = 0
    first_seen: datetime = Field(default_factory=utc_now)
    extracted_at: datetime | None = None


class ResearchSnapshot(BaseModel):
    plan: GoalPlan
    raw: list[Observation] = Field(default_factory=list)
    partial: list[Observation] = Field(default_factory=list)
    accepted: list[Observation] = Field(default_factory=list)
    rejected: list[Observation] = Field(default_factory=list)
    sources: list[SourceOutcome] = Field(default_factory=list)
    files: list[FileLink] = Field(default_factory=list)
    candidate_count: int = 0
    outcome: str = "pending"
    scored_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class WebhookCreate(BaseModel):
    url: str
    events: list[str] = Field(default_factory=lambda: ["run.completed"])
    enabled: bool = True


class WebhookUpdate(BaseModel):
    url: str | None = None
    events: list[str] | None = None
    enabled: bool | None = None


class Page(BaseModel):
    rows: list[Any]
    row_count: int
    limit: int
    offset: int
