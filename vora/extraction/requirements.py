"""Turn a natural-language goal into minimal semantic requirements.

The planner must not confuse *desired concepts* with *mandatory columns*.
Only concepts the user actually asked for become required: typically the
measure ("price", "yield") and, when a time constraint exists, the period.
Everything a model proposes beyond that (make, model, trim, currency...) is
kept as optional enrichment. Time windows are always resolved here,
deterministically, never by a model.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date

from vora.shared.contracts import ConceptRequirement, GoalPlan, TimeScope
from vora.shared.regions import COUNTRY_NAMES
from vora.shared.urls import goal_domains, normalize_domain

from vora.extraction.semantics import IDENTIFIER_HEADS, LEXICON, STOPWORDS, SUBJECT_HEAD_CLASSES, UNIT_TOKENS, canonical, concept_spec, correct_token, similar, token_class, tokens
from vora.extraction.temporal import TimeWindow, resolve_time_window, strip_time_expressions, today_utc

MAX_REQUIRED_MEASURES = 3
# Words that rank or qualify the subject rather than name it ("top cities").
_QUALIFIERS = {"top", "best", "worst", "most", "least", "major", "leading", "largest", "biggest",
               "smallest", "highest", "lowest", "popular", "famous", "main", "total", "overall"}
# Trailing words left behind when a time expression is removed from a query.
_DANGLING = {"in", "of", "for", "from", "over", "during", "since", "between", "the", "by", "to", "and"}
# Wording that asks for numbers even when no measure word is named ("number of
# hospitals", "wins by season", "average across states"): such a request keeps a
# numeric requirement instead of accepting records.
QUANTITATIVE_CUES = re.compile(
    r"\b(?:number of|how many|count of|counts? by|total (?:number|count|of)|percentage|percent(?:age)? of|"
    r"share of|proportion of|ratio|average|mean|median|per capita|per (?:person|household|state|district|"
    r"year|month|day)|by (?:year|month|quarter|week|day|season|decade|state|district|region)|year[- ]on[- ]year|"
    r"over time|time series|trend|growth|statistics|stats)\b", re.I)
# A word after one of these names where the data comes from ("from example").
_SOURCE_MARKERS = {"from", "on", "at", "via", "using", "within", "site", "portal", "website", "database"}
_MONTH_WORDS = {"january", "february", "march", "april", "may", "june", "july", "august",
                "september", "october", "november", "december"}


@dataclass(slots=True)
class DraftConcept:
    name: str
    aliases: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PlannerDraft:
    """What a model may contribute: vocabulary and suggestions, never authority."""

    intent: str = "dataset"
    subject_terms: list[str] = field(default_factory=list)
    measures: list[DraftConcept] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    geography: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    search_queries: list[str] = field(default_factory=list)
    suggested_sources: list[str] = field(default_factory=list)


@dataclass(slots=True)
class GoalAnalysis:
    subject_terms: list[str]
    mentions: list[str]
    measures: list[ConceptRequirement]
    dimensions: list[ConceptRequirement]
    entities: list[str]
    window: TimeWindow | None
    corrected_goal: str


def time_scope(window: TimeWindow | None, today: date) -> TimeScope | None:
    if window is None:
        return None
    return TimeScope(
        expression=window.expression, start=window.start.isoformat(), end=window.end.isoformat(),
        granularity=window.granularity, relative=window.relative, policy=window.policy,
        periods=list(window.periods), resolved_on=today.isoformat(),
    )


def analyze_words(goal: str, today: date | None = None) -> GoalAnalysis:
    """Deterministic reading of a goal into subject, measures and dimensions."""
    today = today or today_utc()
    window = resolve_time_window(goal, today)
    # Links in the goal are sources to open (see vora.research.discovery.discovery), not words.
    remainder = strip_time_expressions(re.sub(r"https?://\S+", " ", goal))
    words = re.findall(r"[A-Za-z][A-Za-z0-9&+.'-]*", remainder)

    subject: list[str] = []
    run: list[str] = []
    measures: dict[str, ConceptRequirement] = {}
    dimensions: dict[str, ConceptRequirement] = {}
    entities: list[str] = []
    mentions: list[str] = []
    fixes: dict[str, str] = {}

    def flush() -> None:
        if len(run) > 1:
            phrase = " ".join(run)
            if phrase not in subject:
                subject.append(phrase)
        run.clear()

    for index, original in enumerate(words):
        word = original.strip(".'-")
        lowered = word.casefold()
        parts = tokens(lowered)
        token = parts[0] if len(parts) == 1 else lowered
        if not token or token in STOPWORDS or lowered in STOPWORDS or token in UNIT_TOKENS \
                or lowered in _MONTH_WORDS:
            flush()
            continue
        corrected = correct_token(token)
        if corrected != token:
            fixes[word] = corrected
        concept_class = token_class(corrected)
        if concept_class == "period":
            flush()
            continue
        if concept_class:
            kind = LEXICON[concept_class][0]
            target = measures if kind == "measure" else dimensions
            requirement = target.setdefault(concept_class, ConceptRequirement(
                name=concept_class, role="required" if kind == "measure" else "optional",
                kind="measure" if kind == "measure" else "dimension", aliases=[], source="goal",
            ))
            for alias in {corrected, token} - {concept_class}:
                if alias not in requirement.aliases:
                    requirement.aliases.append(alias)
            flush()
            continue
        if token not in subject:
            subject.append(token)
        if index > 0 and words[index - 1].casefold() in _SOURCE_MARKERS and len(token) >= 3 \
                and token not in mentions:
            mentions.append(token)
        run.append(token)
        is_acronym = word.isupper() and 2 <= len(word) <= 6
        is_proper = word[:1].isupper() and not word.isupper() and index > 0
        if (is_acronym or is_proper) and word not in entities:
            entities.append(word)
    flush()
    return GoalAnalysis(
        subject_terms=subject[:12], mentions=mentions[:4], measures=list(measures.values()),
        dimensions=list(dimensions.values()), entities=entities[:8], window=window,
        # The request as written (time words kept), with spelling fixed.
        corrected_goal=re.sub(r"[A-Za-z][A-Za-z0-9&+.'-]*",
                              lambda match: fixes.get(match.group(0).strip(".'-"), match.group(0)),
                              " ".join(goal.split())),
    )


def _grounded(name: str, aliases: Sequence[str], goal_tokens: set[str], goal_concepts: set[str]) -> bool:
    """A proposed concept is grounded if the user's words actually mention it."""
    spec = concept_spec(name, aliases=aliases)
    if spec.name in goal_concepts:
        return True
    candidates = [spec.name, *(" ".join(alias) for alias in spec.aliases)]
    for candidate in candidates:
        parts = [part for part in tokens(candidate) if part not in UNIT_TOKENS]
        if parts and all(any(similar(part, word) >= 0.85 for word in goal_tokens) for part in parts):
            return True
    return False


def build_requirements(goal: str, *, draft: PlannerDraft | None = None,
                       today: date | None = None) -> dict:
    """Return GoalPlan fields describing the minimum requirements of ``goal``."""
    today = today or today_utc()
    analysis = analyze_words(goal, today)
    goal_tokens = {correct_token(token) for token in tokens(strip_time_expressions(goal))}
    goal_concepts = canonical(strip_time_expressions(goal))
    notes: list[str] = []

    measures = {item.name: item for item in analysis.measures}
    optional: dict[str, ConceptRequirement] = {item.name: item for item in analysis.dimensions}

    if draft:
        for proposed in draft.measures:
            spec = concept_spec(proposed.name, aliases=proposed.aliases)
            if spec.kind == "time":
                continue
            aliases = [alias for alias in [proposed.name, *proposed.aliases]
                       if alias and alias.casefold() != spec.name][:12]
            if _grounded(proposed.name, proposed.aliases, goal_tokens, goal_concepts) and spec.kind == "measure":
                target = measures.setdefault(spec.name, ConceptRequirement(
                    name=spec.name, role="required", kind="measure", source="planner"))
            else:
                target = optional.setdefault(spec.name, ConceptRequirement(
                    name=spec.name, role="optional",
                    kind="measure" if spec.kind == "measure" else "dimension", source="planner"))
            for alias in aliases:
                if alias not in target.aliases and len(target.aliases) < 20:
                    target.aliases.append(alias)
        for name in draft.dimensions:
            spec = concept_spec(name)
            if spec.kind == "time" or spec.name in measures:
                continue
            optional.setdefault(spec.name, ConceptRequirement(
                name=spec.name, role="optional",
                kind="dimension" if spec.kind != "measure" else "measure",
                aliases=[name] if name.casefold() != spec.name else [], source="planner"))

    required = list(measures.values())[:MAX_REQUIRED_MEASURES]
    for extra in list(measures.values())[MAX_REQUIRED_MEASURES:]:
        optional.setdefault(extra.name, extra.model_copy(update={"role": "optional"}))
    answer_shape = "quantities"
    if not required and QUANTITATIVE_CUES.search(goal):
        # "number of hospitals", "wins by season": numbers are asked for even though
        # no measure word names them, so any quantity about the subject counts.
        required = [ConceptRequirement(name="value", role="required", kind="measure",
                                       aliases=["amount", "figure", "total"], source="ontology")]
        notes.append("The request asks for numbers without naming a measure; any quantity about the "
                     "subject counts.")
    elif not required:
        # No measure word and no wording that asks for numbers: the request may want
        # records ("gazette notices", "AI conferences") or numbers about the subject.
        # Nothing numeric is invented as required; the scorer accepts either kind.
        answer_shape = "either"
        notes.append("No measure named in the request: well-formed records about the subject, "
                     "and rows with a number about it, are both accepted.")
    for item in required:
        optional.pop(item.name, None)

    concepts = list(required)
    if analysis.window:
        concepts.append(ConceptRequirement(name="period", role="required", kind="time",
                                           aliases=[analysis.window.expression], source="goal"))
        notes.append(f"Time window: {analysis.window.policy}.")
    concepts.extend(item.model_copy(update={"role": "optional"}) for item in optional.values())

    subject = list(analysis.subject_terms)
    if draft:
        for term in draft.subject_terms:
            cleaned = " ".join(term.split()).casefold()
            if cleaned and cleaned not in subject and len(subject) < 20:
                subject.append(cleaned)
    geography = list(dict.fromkeys(term for term in (draft.geography if draft else []) if term))[:8]
    entities = list(dict.fromkeys([*(draft.entities if draft else []), *analysis.entities]))[:8]

    heads: list[str] = []
    if answer_shape == "either" or any(item.name in SUBJECT_HEAD_CLASSES for item in required):
        places = {term.casefold() for term in [*geography, *entities]} | COUNTRY_NAMES
        for term in analysis.subject_terms:
            if " " in term or len(term) < 3 or term in places or term in IDENTIFIER_HEADS \
                    or term in _QUALIFIERS or token_class(term):
                continue
            if term not in heads:
                heads.append(term)

    scope = time_scope(analysis.window, today)
    # A website written in the request ("example.com") is the source the user named: it is read first,
    # and its own name ("example") is what matches other addresses of the same site (example.gov.in).
    domains = goal_domains(goal)
    named = [domain.split(".")[0] for domain in domains if len(domain.split(".")[0]) >= 3]
    suggested: list[str] = list(domains[:6])
    for value in (draft.suggested_sources if draft else []):
        try:
            domain = normalize_domain(value)
        except ValueError:
            continue
        if domain not in suggested and len(suggested) < 6:
            suggested.append(domain)
    return {
        "suggested_sources": suggested,
        "subject_terms": subject,
        "subject_heads": heads[:12],
        "answer_shape": answer_shape,
        "source_mentions": list(dict.fromkeys([*named, *analysis.mentions, *analysis.entities]))[:6],
        "concepts": concepts[:24],
        "required_fields": [item.name for item in concepts if item.role == "required"],
        "time_scope": scope,
        "year_start": analysis.window.start.year if analysis.window else None,
        "year_end": analysis.window.end.year if analysis.window else None,
        "geography": geography,
        "entities": entities,
        "notes": notes,
        "corrected_goal": analysis.corrected_goal,
    }


def align_query_years(query: str, scope: TimeScope | None) -> str:
    """Replace year ranges a model wrote into a query with the resolved window."""
    if scope is None or scope.granularity != "year":
        return query
    start, end = scope.start[:4], scope.end[:4]
    replacement = start if start == end else f"{start}-{end}"
    pattern = r"\b(?:19|20)\d{2}\s*(?:-|–|to|\.\.)\s*(?:19|20)\d{2}\b"
    if re.search(pattern, query):
        return re.sub(pattern, replacement, query)
    return query


def _trim(text: str) -> str:
    """Drop words a removed time expression left dangling ("yield in" -> "yield")."""
    words = text.split()
    while words and words[-1].casefold() in _DANGLING:
        words.pop()
    while words and words[0].casefold() in _DANGLING:
        words.pop(0)
    return " ".join(words)


def default_queries(goal: str, scope: TimeScope | None, answer_shape: str = "quantities",
                    mentions: Sequence[str] = ()) -> list[str]:
    """Search queries built from the user's own words.

    The first keeps the request as written (with its years); the others add
    the resolved window, so no query loses the time the user asked about.
    Requests for numbers add "data table"-style words; requests for records
    look up each named source and add "official", "list" and "latest".
    """
    base = " ".join(goal.split())
    stripped = _trim(strip_time_expressions(base)) or base
    suffix = ""
    if scope is not None and scope.granularity == "year":
        suffix = f" {scope.start[:4]}-{scope.end[:4]}" if scope.start[:4] != scope.end[:4] else f" {scope.start[:4]}"
    elif scope is not None:
        suffix = f" {scope.end[:4]}"
    if answer_shape == "quantities":
        queries = [base, f"{stripped}{suffix} data table", f"{stripped}{suffix} statistics",
                   f"{stripped}{suffix} dataset"]
    else:
        queries = [base, *(f"{mention} official website" for mention in mentions[:2]),
                   f"{stripped}{suffix} official", f"{stripped}{suffix} list", f"{stripped}{suffix} latest"]
    return list(dict.fromkeys(query.strip() for query in queries if query.strip()))[:4]


def upgrade_plan(plan: GoalPlan, goal: str | None = None, today: date | None = None) -> GoalPlan:
    """Rebuild concept requirements for plans stored before concepts existed.

    Legacy plans carried model-invented mandatory columns and model-computed
    years. Those fields are re-read as suggestions and re-grounded against the
    user's original goal.
    """
    source_goal = goal or plan.normalized_goal
    draft = PlannerDraft(
        measures=[DraftConcept(name) for name in plan.required_fields],
        subject_terms=list(plan.subject_terms),
        geography=list(plan.geography), entities=list(plan.entities),
        suggested_sources=list(plan.suggested_sources),
    )
    fields = build_requirements(source_goal, draft=draft, today=today)
    fields.pop("corrected_goal", None)
    return plan.model_copy(update={**fields, "normalized_goal": " ".join(source_goal.split()),
                                   "planner": plan.planner if plan.concepts else "legacy-upgraded"})


def ensure_concepts(plan: GoalPlan, today: date | None = None) -> GoalPlan:
    return plan if plan.concepts else upgrade_plan(plan, today=today)


def requirement_specs(items: Iterable[ConceptRequirement]):
    return [concept_spec(item.name, kind=item.kind if item.kind != "topic" else None,
                         aliases=item.aliases) for item in items]
