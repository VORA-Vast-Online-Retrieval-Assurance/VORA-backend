"""Multi-factor observation scoring.

Replaces "required field missing -> reject" with a graded judgement:

    score = 0.30 coverage      how well required concepts are expressed
          + 0.22 relevance     is the observation about the requested subject
          + 0.18 temporal      does its period fall inside the requested window
          + 0.12 extraction    how reliable the extraction method was
          + 0.10 completeness  optional enrichment and field richness
          + 0.08 source        provenance and publisher quality
          - 0.60 * noise       structural noise probability

Coverage carries the most weight because a row without the requested measure
cannot answer the question however relevant its page is. Hard gates keep the
accepted dataset honest: a row outside the requested window, or one with no
evidence of the requested measure, can be *partial* but never *accepted*.

Tiers: high >= 0.72, usable >= 0.55 (both accepted); partial >= 0.38;
low below that; noise for chrome, metadata and challenge pages.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import date
from urllib.parse import urlparse

from vora.shared.contracts import ConceptRequirement, GoalPlan, Observation

from vora.extraction.noise import assess
from vora.extraction.blocks import apply_block_support
from vora.extraction.parser import METHOD_CONFIDENCE, STATEMENT_METHODS
from vora.extraction.records import RecordVerdict, assess_record
from vora.extraction.requirements import ensure_concepts, requirement_specs
from vora.extraction.semantics import ADDRESS_WORDS, GENERIC_FIELD_NAMES, MODIFIERS, STOPWORDS, ConceptMatch, ConceptMatcher, ConceptSpec, content_tokens, has_quantity, humanize, similar, time_field_names, token_class, tokens, value_kind
from vora.extraction.temporal import TemporalResolution, parse_period, resolve_observation_period, today_utc

WEIGHTS = {
    "coverage": 0.30,
    "relevance": 0.22,
    "temporal": 0.18,
    "extraction": 0.12,
    "completeness": 0.10,
    "source": 0.08,
}
NOISE_WEIGHT = 0.60
HIGH, USABLE, PARTIAL = 0.72, 0.55, 0.38
MEASURE_EVIDENCE = 0.45
RELEVANCE_GATE = 0.35

FieldMapper = Callable[[list[str], list[str]], Mapping[str, str | None]]


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


def _initials(words: list[str]) -> set[str]:
    found = set()
    for size in (2, 3, 4):
        for index in range(len(words) - size + 1):
            found.add("".join(word[0] for word in words[index:index + size]))
    return found


class _Text:
    """Pre-tokenised text used for relevance checks."""

    __slots__ = ("tokens", "initials", "joined")

    def __init__(self, value: str) -> None:
        words = [token for token in tokens(value) if not token.isdigit()]
        self.tokens = set(words)
        self.initials = _initials(words)
        # Names written as one word are split by camelCase ("eExample" -> e, example);
        # adjacent tokens rejoined match them again.
        self.joined = {first + second for first, second in zip(words, words[1:])}

    def contains(self, term_tokens: tuple[str, ...]) -> bool:
        if not term_tokens:
            return False
        if len(term_tokens) == 1:
            term = term_tokens[0]
            if term in self.tokens or term in self.joined:
                return True
            if 2 <= len(term) <= 4 and term in self.initials:
                return True  # "ev" ~ "electric vehicle"
            if 2 <= len(term) <= 3 and any(len(word) <= len(term) + 2 and word.endswith(term) for word in self.tokens):
                return True  # acronym subtypes: "bev", "phev" ~ "ev"
            if len(term) >= 5:
                return any(similar(term, word) >= 0.85 for word in self.tokens if abs(len(word) - len(term)) <= 4)
            return False
        if all(self.contains((part,)) for part in term_tokens):
            return True
        acronym = "".join(part[0] for part in term_tokens)
        return 2 <= len(acronym) <= 4 and acronym in self.tokens


def source_quality(observation: Observation) -> float:
    host = (urlparse(observation.source_url).hostname or "").casefold()
    labels = host.split(".")
    score = 0.55
    if any(label in {"gov", "gouv", "gob", "govt", "edu", "ac", "int", "mil"} for label in labels[1:]):
        score = 0.85
    elif host.endswith(".org"):
        score = 0.65
    if observation.published_at or observation.modified_at:
        score += 0.05
    if observation.source_title:
        score += 0.05
    return min(1.0, score)


def attribution(sentence: str, concept: ConceptSpec, subject: list[tuple[str, ...]],
                context: str = "") -> str:
    """Who a measure word in prose belongs to: "subject", "foreign" or "unknown".

    "gasoline prices are higher" attributes the price to gasoline; "BEV pack
    prices fell" and "the average price of a used EV" attribute it to the
    subject. A modifier the page itself is about (it appears in the heading
    or title, as "pack" does under "Battery pack prices") also counts as on
    subject. Only the words right around the measure word are considered.
    """
    page_words = set(tokens(context))
    words = [token for token in tokens(sentence) if not token.isdigit()]
    verdicts = []
    for index, word in enumerate(words):
        if not (word == concept.name or (word,) in concept.aliases or
                (concept.lexicon_class and token_class(word) == concept.lexicon_class)):
            continue
        before = [token for token in words[max(0, index - 4):index]
                  if token not in STOPWORDS and token not in MODIFIERS][-2:]
        if not before:
            verdicts.append("unknown")
            continue
        window = _Text(" ".join(before))
        after = _Text(" ".join(words[index + 1:index + 6]))  # "price of a used EV"
        on_subject = any(window.contains(term) or after.contains(term) for term in subject)
        verdicts.append("subject" if on_subject or set(before) & page_words else "foreign")
    if not verdicts:
        return "unknown"
    return "foreign" if all(verdict == "foreign" for verdict in verdicts) else "subject"


# The parser's form for counts in prose: "3,961 villages", "1,280 hospitals".
_COUNTED = re.compile(r"^[\d,]+\s+([a-z]+)$", re.I)


class ObservationScorer:
    """Score raw observations against a plan's semantic requirements."""

    def __init__(self, plan: GoalPlan, *, today: date | None = None, block_support: bool = True) -> None:
        self.plan = ensure_concepts(plan, today)
        # Judge tables and card groups as a whole as well (vora.extraction.blocks).
        self.block_support = block_support
        self.today = today or today_utc()
        required = [item for item in self.plan.required_concepts if item.kind != "time"]
        if self.plan.answer_shape == "either" and not required:
            # The request names no measure: numbers about the subject are one kind of
            # answer (records are the other). This is scoring machinery, not a
            # requirement the user sees.
            required = [ConceptRequirement(name="value", role="required", kind="measure",
                                           aliases=["amount", "figure", "total"], source="ontology")]
        optional = [item for item in self.plan.optional_concepts if item.kind != "time"]
        self.required = requirement_specs(required)
        self.optional = requirement_specs(optional)
        self.matcher = ConceptMatcher([*self.required, *self.optional], self.plan.subject_heads)
        scope = self.plan.time_scope
        self.window: tuple[date, date] | None = None
        if scope and _date(scope.start) and _date(scope.end):
            self.window = (_date(scope.start), _date(scope.end))
        elif self.plan.year_start is not None and self.plan.year_end is not None:
            self.window = (date(self.plan.year_start, 1, 1), date(self.plan.year_end, 12, 31))
        self.window_label = ""
        if self.window:
            start, end = self.window
            self.window_label = str(start.year) if start.year == end.year else f"{start.year}–{end.year}"
            if scope and scope.granularity not in {"year"}:
                self.window_label = f"{start.isoformat()} – {end.isoformat()}"
        self.subject = [tuple(word for word in content_tokens(term) if word not in ADDRESS_WORDS)
                        for term in self.plan.subject_terms]
        self.subject = [term for term in self.subject if term]
        self.constraints = [tuple(content_tokens(term)) for term in [*self.plan.geography, *self.plan.entities]]
        self.constraints = [term for term in self.constraints if term]
        # A named source: a site the request points at, not a place or an acronym it happens to mention.
        places = {name.casefold() for name in [*self.plan.entities, *self.plan.geography]}
        self.named_source = any(name.casefold() not in places for name in self.plan.source_mentions)

    # -- components ---------------------------------------------------------

    def _relevance(self, observation: Observation) -> tuple[float, list[str]]:
        if not self.subject:
            return 0.7, []
        path = urlparse(observation.source_url).path.replace("-", " ").replace("_", " ").replace("/", " ")
        locations = (
            (_Text(" ".join(f"{key} {value}" for key, value in observation.fields.items()
                            if key not in {"url", "image"})), 1.0),
            (_Text(observation.context), 0.9),
            # A prose page discusses many things, so for sentences the page
            # title alone is weak evidence that this sentence is on-subject.
            (_Text(f"{observation.source_title} {path} {(urlparse(observation.source_url).hostname or '').replace('.', ' ')}"),
             0.4 if observation.method in STATEMENT_METHODS else 0.8),
        )
        best, hits = 0.0, []
        for text, weight in locations:
            matched = [term for term in self.subject if text.contains(term)]
            if not matched:
                continue
            share = len(matched) / min(len(self.subject), 3)
            value = (0.65 + 0.35 * min(1.0, share)) * weight
            if value > best:
                best, hits = value, [" ".join(term) for term in matched]
        if self.constraints:
            everything = _Text(" ".join([*observation.fields.values(), observation.context,
                                         observation.source_title, path]))
            if any(everything.contains(term) for term in self.constraints):
                best = min(1.0, best + 0.1)
        return round(best, 3), hits

    def _counts(self, noun: str, concept: ConceptSpec) -> bool:
        """Whether "N <noun>" is a count of what ``concept`` measures."""
        word = (tokens(noun) or (noun,))[0]
        if concept.name == "value" and not self.matcher.subject_heads:
            return True  # nothing to compare with: any count may be the requested one
        if concept.lexicon_class and token_class(word) == concept.lexicon_class:
            return True
        names = [concept.name, *(part for alias in concept.aliases for part in alias), *self.matcher.subject_heads]
        return any(similar(word, name) >= 0.85 for name in names)

    def _temporal(self, observation: Observation, time_keys: list[str],
                  has_measure: bool, measure_field: str | None = None) -> tuple[TemporalResolution, float, bool]:
        # A row whose keys are periods is a time series; it has no single period.
        period_keys = sum(1 for key in observation.fields if parse_period(key.replace("_", "/")))
        has_measure = has_measure and period_keys < 3
        resolution = resolve_observation_period(
            observation.fields, time_keys,
            context=observation.context, context_kind=observation.context_kind,
            page_title=observation.source_title, temporal_coverage=observation.temporal_coverage,
            published_at=observation.published_at, modified_at=observation.modified_at,
            fetched_at=observation.fetched_at, has_measure=has_measure, today=self.today,
            measure_field=measure_field,
        )
        period = resolution.period
        if self.window is None:
            return resolution, 1.0 if period else 0.75, False
        if period is None:
            return resolution, 0.2, False
        start, end = self.window
        if not period.overlaps(start, end):
            return resolution, 0.0, True
        fit = 0.6 + 0.4 * resolution.confidence
        if not period.within(start, end):
            fit *= 0.8  # e.g. a 2010–2025 range against a 2017–2026 window
        return resolution, round(fit, 3), False

    def _extraction(self, observation: Observation) -> float:
        prior = observation.extraction_confidence or METHOD_CONFIDENCE.get(observation.method, 0.5)
        names = list(observation.fields)
        generic = sum(1 for name in names if GENERIC_FIELD_NAMES.match(name)) / max(1, len(names))
        penalty = 0.25 * generic + (0.1 if len(names) <= 1 else 0.0)
        return round(max(0.05, prior - penalty), 3)

    # -- scoring ------------------------------------------------------------

    def score(self, observation: Observation) -> Observation:
        verdict = assess(observation.fields, observation.method, observation.content_role)
        if verdict.role != "data":
            return observation.model_copy(update={
                "status": "rejected", "tier": "noise", "content_role": verdict.role,
                "noise_probability": verdict.probability, "score": 0.0,
                "reasons": [verdict.reason], "score_breakdown": {"noise": verdict.probability},
            })

        fields = observation.fields
        time_keys = time_field_names(fields)
        reasons: list[str] = []
        matches: dict[str, ConceptMatch] = {}

        credits = []
        for concept in self.required:
            match = self.matcher.match(fields, concept, exclude=time_keys, context=observation.context)
            credits.append(match.credit if match else 0.0)
            if match and match.credit >= MEASURE_EVIDENCE:
                matches[concept.name] = match
                reasons.append(f"'{concept.name}' ← {match.field} ({match.via}, {match.credit:.2f})")
            elif match:
                # Weak evidence counts toward the score but is never shown as the value.
                reasons.append(f"Only weak evidence for '{concept.name}' ({match.field}, {match.credit:.2f})")
            else:
                reasons.append(f"No value for '{concept.name}'")
        statement = fields.get("statement", "") if observation.method in STATEMENT_METHODS else ""
        if statement and self.subject:
            for position, concept in enumerate(self.required):
                if (concept.is_measure and concept.name in matches
                        and attribution(statement, concept, self.subject,
                                        f"{observation.context} {observation.source_title}") == "foreign"):
                    # The sentence's measure belongs to something else.
                    credits[position] = min(credits[position], 0.3)
                    matches.pop(concept.name)
                    reasons.append(f"'{concept.name}' in this sentence refers to something other than the subject")
        if observation.method == "undated_statement" and self.matcher.subject_heads and not any(
                similar(token, head) >= 0.85 for token in tokens(statement) for head in self.matcher.subject_heads):
            # Without a date or a table around it, a sentence counts only if it
            # names what the user counts ("1,280 hospitals"), not any figure nearby.
            for position, concept in enumerate(self.required):
                if concept.is_measure and concept.name in matches:
                    credits[position] = min(credits[position], 0.3)
                    matches.pop(concept.name)
            reasons.append("An undated sentence must name what is counted")
        if statement:
            for position, concept in enumerate(self.required):
                match = matches.get(concept.name)
                counted = _COUNTED.match(fields.get(match.field, "").strip()) if match and concept.is_measure else None
                if counted and not self._counts(counted.group(1), concept):
                    # "There are 3,961 villages" counts villages, not the population.
                    credits[position] = min(credits[position], 0.3)
                    matches.pop(concept.name)
                    reasons.append(f"The sentence counts {counted.group(1)}, not '{concept.name}'")
        row_has_quantity = any(has_quantity(value) for key, value in fields.items() if key not in time_keys)
        if self.required:
            coverage = sum(credits) / len(self.required)
            measure_ok = all(credit >= MEASURE_EVIDENCE for concept, credit in zip(self.required, credits)
                             if concept.is_measure) and row_has_quantity
        else:
            coverage = 1.0 if row_has_quantity else 0.4
            measure_ok = row_has_quantity and self.plan.answer_shape != "records"
        if self.plan.answer_shape == "either" and observation.method in {"undated_statement", "text_statement"}:
            # Nothing in the request asked for a number, so a figure inside a sentence
            # ("costs $2,695", "5G") is not an answer; only a record can be.
            measure_ok = False
        # The other kind of answer: a well-formed record (something that names it,
        # plus typed attributes such as a date, a link or an identifier).
        record: RecordVerdict | None = None
        if self.plan.answer_shape in {"records", "either"} and not measure_ok:
            record = assess_record(fields)
        optional_credits = []
        for concept in self.optional:
            match = self.matcher.match(fields, concept, exclude=[*time_keys, *(m.field for m in matches.values())],
                                       context=observation.context)
            optional_credits.append(match.credit if match else 0.0)
            if match and match.credit >= 0.75:
                matches[concept.name] = match
        filled = sum(1 for value in fields.values() if str(value).strip())
        richness = min(1.0, filled / 4)
        completeness = (0.5 * (sum(optional_credits) / len(optional_credits)) + 0.5 * richness
                        if optional_credits else 0.2 + 0.8 * richness)

        relevance, subject_hits = self._relevance(observation)
        if self.subject and not subject_hits:
            reasons.append("Subject not mentioned in the row, its context, or the page")
        measure_field = next((matches[concept.name].field for concept in self.required
                              if concept.is_measure and concept.name in matches), None)
        resolution, temporal, out_of_range = self._temporal(observation, time_keys, row_has_quantity, measure_field)
        record_ok = bool(record and record.ok)
        if record_ok and self.window and (resolution.period is None or resolution.inferred):
            # A date is needed to check the requested window, and it has to be the row's own.
            # One borrowed from the page ("...2026" in a title or heading) says nothing about it.
            record_ok = False
            reasons.append("No date in the record to check against the requested "
                           f"{self.window_label}")
        if record_ok:
            coverage = max(coverage, record.coverage)
            reasons.append(f"Record: {record.summary}")
            matches["record"] = ConceptMatch("record", record.title_field or "", record.coverage, "record")
        if resolution.period:
            detail = f"Period {resolution.period.label} ({resolution.note.lower()}"
            detail += f", inferred, confidence {resolution.confidence:.2f})" if resolution.inferred else ")"
            reasons.append(detail)
        elif self.window:
            reasons.append("No period stated for the requested window")
        if out_of_range:
            reasons.append(f"Period {resolution.period.label} is outside the requested {self.window_label}")

        extraction = self._extraction(observation)
        source = source_quality(observation)
        components = {
            "coverage": round(coverage, 3), "relevance": relevance, "temporal": temporal,
            "extraction": extraction, "completeness": round(completeness, 3), "source": round(source, 3),
        }
        score = sum(WEIGHTS[name] * value for name, value in components.items())
        score -= NOISE_WEIGHT * verdict.probability
        score = round(max(0.0, min(1.0, score)), 3)
        components["noise"] = round(verdict.probability, 3)
        if verdict.reason:
            reasons.append(verdict.reason)

        subject_ok = not self.subject or relevance >= RELEVANCE_GATE
        gates = (measure_ok or record_ok) and not out_of_range and subject_ok
        if gates and score >= HIGH:
            tier = "high"
        elif gates and score >= USABLE:
            tier = "usable"
        elif score >= PARTIAL and (row_has_quantity or (record and record.partial)) \
                and (measure_ok or record_ok or relevance >= 0.5):
            tier = "partial"
        else:
            tier = "low"
        if not (measure_ok or record_ok) and self.required:
            reasons.append("Not a well-formed record and no value for the requested measure"
                           if self.plan.answer_shape == "either" else "Missing evidence for the requested measure")

        return observation.model_copy(update={
            # When the request names a source, a row that never mentions what was asked for is not a
            # partial answer, it is from somewhere else: it is set aside, not kept for review.
            "status": "accepted" if tier in {"high", "usable"}
                      else "rejected" if self.subject and self.named_source and not subject_hits else "partial",
            "extraction_confidence": observation.extraction_confidence or METHOD_CONFIDENCE.get(observation.method, 0.5),
            "tier": tier,
            "score": score,
            "quality_score": score,
            "relevance_score": relevance,
            "score_breakdown": components,
            "concept_matches": {name: match.as_dict() for name, match in matches.items()},
            "matched_fields": list(matches),
            "normalized": (self._normalize_record(fields, record) if record_ok
                           else self._normalize(fields, time_keys, matches, resolution)),
            "reasons": reasons,
            "noise_probability": verdict.probability,
            "data_period": resolution.period.label if resolution.period else None,
            "period_start": resolution.period.start.isoformat() if resolution.period else None,
            "period_end": resolution.period.end.isoformat() if resolution.period else None,
            "period_granularity": resolution.period.granularity if resolution.period else None,
            "time_basis": resolution.basis,
            "time_inferred": resolution.inferred,
            "temporal_confidence": resolution.confidence,
        })

    @staticmethod
    def _normalize_record(fields: Mapping[str, str], record: RecordVerdict) -> dict[str, str]:
        """A record keeps its own field names (its date stays "issue_date"), the
        naming field first; nothing is renamed to period, series or value."""
        title = record.title_field
        ordered = {title: fields[title]} if title in fields else {}
        ordered.update({key: value for key, value in fields.items()
                        if key != title and key != "series" and str(value).strip()})
        return ordered

    def _normalize(self, fields: Mapping[str, str], time_keys: list[str],
                   matches: Mapping[str, ConceptMatch], resolution: TemporalResolution) -> dict[str, str]:
        """Tidy record: period, series, requested concepts, optional concepts, rest."""
        consumed = set(time_keys) | {"series", "label"}
        series = fields.get("series") or fields.get("label") or ""
        concepts: dict[str, str] = {}
        for concept in self.required:
            match = matches.get(concept.name)
            if not match:
                continue
            concepts[concept.name] = fields[match.field]
            consumed.add(match.field)
            # Keep what was actually measured when the header was more specific
            # than the concept ("average pack price" behind "price").
            if (not series and match.via not in {"value_type", "text"}
                    and tokens(match.field) != (concept.name,) and not GENERIC_FIELD_NAMES.match(match.field)):
                series = humanize(match.field)
        for concept in self.optional:
            match = matches.get(concept.name)
            if match and match.field not in consumed and concept.name not in concepts:
                concepts[concept.name] = fields[match.field]
                consumed.add(match.field)
        record: dict[str, str] = {}
        if resolution.period:
            record["period"] = resolution.period.label
        if series:
            record["series"] = series
        record.update(concepts)
        for key, value in fields.items():
            if key not in consumed and key not in record and value_kind(value) != "empty":
                record[key] = value
        return record

    def score_all(self, rows: Iterable[Observation]) -> tuple[list[Observation], list[Observation], list[Observation]]:
        accepted, partial, rejected = [], [], []
        for row in rows:
            scored = self.score(row)
            (accepted if scored.status == "accepted" else rejected if scored.status == "rejected" else partial).append(scored)
        if self.block_support:
            return apply_block_support(accepted, partial, rejected)
        return accepted, partial, rejected

    def learn_from(self, rows: Iterable[Observation], mapper: FieldMapper) -> bool:
        """Ask ``mapper`` (e.g. a model) about headers the lexicon could not place.

        Only relevant rows that lack the requested measure are considered, and
        each header is only ever resolved once per scorer.
        """
        candidates = [row.fields for row in rows
                      if row.tier in {"partial", "low"} and row.relevance_score >= 0.5
                      and row.content_role == "data"]
        headers = self.matcher.unresolved_headers(candidates)[:30]
        if not headers or not self.required:
            return False
        mapping = mapper(headers, [concept.name for concept in [*self.required, *self.optional]])
        self.matcher.learn(mapping or {})
        return bool(mapping and any(mapping.values()))


def classify_observations(rows: list[Observation], plan: GoalPlan,
                          today: date | None = None) -> tuple[list[Observation], list[Observation], list[Observation]]:
    """Score rows and split them into (accepted, partial, rejected)."""
    return ObservationScorer(plan, today=today).score_all(rows)
