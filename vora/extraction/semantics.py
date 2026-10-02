"""Domain-neutral semantic normalization of field names and values.

Field names found on the web rarely equal the words a user typed. This module
maps raw names such as ``average_pack_price`` or ``yield_t_ha`` to canonical
concepts through a layered, deterministic pipeline:

    raw field -> canonical tokens -> lexicon class / alias / fuzzy match
              -> value-type evidence -> (optional) cached model mapping

The lexicon only describes the *generic vocabulary of quantitative data*
(time, money, quantities, rates, geography, identity...). It deliberately
contains no subject-matter terms: topic words such as "battery", "wheat" or
"unemployment" match literally, by stem, or through aliases the planner
supplies for a specific request.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher, get_close_matches
from functools import lru_cache

from vora.extraction.temporal import parse_period

# ----------------------------------------------------------------------------
# Generic lexicon: concept class -> (kind, member tokens)
# ----------------------------------------------------------------------------

LEXICON: dict[str, tuple[str, frozenset[str]]] = {
    "period": ("time", frozenset({
        "year", "yr", "date", "period", "time", "month", "quarter", "qtr", "week", "day",
        "fy", "fiscal", "season", "decade", "timestamp", "datetime", "asof", "vintage",
    })),
    "price": ("measure", frozenset({
        "price", "cost", "msrp", "fare", "fee", "tariff", "charge", "premium", "rent",
        "spend", "spending", "expenditure", "expense", "payment", "wage", "salary",
        "pay", "valuation", "rrp", "mrp", "listprice", "saleprice",
    })),
    "revenue": ("measure", frozenset({
        "revenue", "sale", "turnover", "income", "earning", "profit", "proceeds", "billing",
        "gdp", "receipts",
    })),
    "value": ("measure", frozenset({
        "value", "amount", "total", "sum", "figure", "level", "measurement", "reading",
        "score", "index", "estimate", "stat", "statistic", "metric", "result",
    })),
    "quantity": ("measure", frozenset({
        "quantity", "qty", "count", "number", "num", "unit", "volume", "tonnage", "tonne",
        "ton", "weight", "stock", "inventory", "capacity", "size",
    })),
    "production": ("measure", frozenset({
        "production", "output", "harvest", "generation", "shipment", "delivery",
        "supply", "produced", "manufactured", "throughput", "registration",
    })),
    "yield": ("measure", frozenset({"yield", "productivity", "efficiency"})),
    "rate": ("measure", frozenset({
        "rate", "percent", "percentage", "pct", "share", "ratio", "proportion", "fraction",
        "penetration", "incidence", "prevalence",
    })),
    "change": ("measure", frozenset({
        "change", "growth", "delta", "increase", "decrease", "decline", "diff",
        "difference", "yoy", "cagr", "variation",
    })),
    "population": ("measure", frozenset({"population", "inhabitant", "resident", "headcount", "people", "person"})),
    "geography": ("dimension", frozenset({
        "country", "region", "state", "province", "city", "location", "market", "geography",
        "nation", "territory", "county", "district", "continent", "area", "zone", "locale",
        "place", "municipality", "geo",
    })),
    "entity": ("dimension", frozenset({
        "name", "title", "item", "product", "company", "brand", "make", "manufacturer",
        "model", "organization", "organisation", "firm", "vendor", "maker", "entity",
        "player", "team", "label", "series",
    })),
    "category": ("dimension", frozenset({
        "category", "type", "class", "segment", "kind", "group", "sector", "variety",
        "genre", "tier", "grade", "family", "subtype", "variant", "trim",
    })),
    "unit": ("dimension", frozenset({"currency", "uom", "denomination", "measure"})),
}

# Partial credit between related classes. A generic "value" column can carry a
# price, but that is weaker evidence than a column literally named "cost".
RELATED: dict[frozenset[str], float] = {
    frozenset({"value", "price"}): 0.55, frozenset({"value", "revenue"}): 0.55,
    frozenset({"value", "quantity"}): 0.55, frozenset({"value", "production"}): 0.5,
    frozenset({"value", "yield"}): 0.5, frozenset({"value", "rate"}): 0.45,
    frozenset({"value", "population"}): 0.5, frozenset({"quantity", "production"}): 0.6,
    frozenset({"quantity", "population"}): 0.5, frozenset({"production", "yield"}): 0.45,
    frozenset({"rate", "change"}): 0.5, frozenset({"price", "revenue"}): 0.35,
    frozenset({"entity", "category"}): 0.4,
}

MONETARY = {"price", "revenue"}
MEASURE_CLASSES = {name for name, (kind, _) in LEXICON.items() if kind == "measure"}

_MEMBER_TO_CLASS = {member: name for name, (_, members) in LEXICON.items() for member in members}
_VOCABULARY = sorted(_MEMBER_TO_CLASS)

# Tokens that qualify a field without changing what it measures.
MODIFIERS = frozenset({
    "average", "avg", "mean", "median", "min", "minimum", "max", "maximum", "est",
    "estimated", "approx", "approximate", "annual", "annually", "monthly", "yearly",
    "weekly", "daily", "nominal", "real", "adjusted", "net", "gross", "base", "starting",
    "current", "latest", "new", "old", "used", "per", "in", "of", "the", "a", "an", "and",
    "or", "by", "for", "at", "to", "on", "from", "with", "without", "incl", "excl",
    "including", "excluding", "approximately", "global", "world", "national", "local",
})

UNIT_TOKENS = frozenset({
    "usd", "eur", "gbp", "inr", "jpy", "cny", "rmb", "cad", "aud", "chf", "krw", "brl",
    "rs", "dollar", "euro", "pound", "rupee", "yen", "yuan", "kwh", "mwh", "gwh", "twh",
    "kw", "mw", "gw", "wh", "kg", "g", "mg", "lb", "lbs", "t", "mt", "kt", "ha",
    "hectare", "acre", "km", "mi", "mile", "m", "cm", "mm", "l", "litre", "liter", "gal",
    "gallon", "bbl", "barrel", "bn", "mn", "k", "million", "billion", "thousand",
    "trillion", "crore", "lakh", "sq", "ft", "mph", "kmh", "kph", "kmph", "ml", "inch",
    "celsius", "fahrenheit", "mb", "gb", "tb", "mbps", "hz", "mhz", "ghz",
})

# Heads that label a row rather than measure anything ("Hospital ID", "Rank").
IDENTIFIER_HEADS = frozenset({
    "rank", "ranking", "id", "no", "code", "serial", "sr", "sno", "slno", "position", "pos",
    "pin", "pincode", "zip", "zipcode", "postcode", "phone", "tel", "fax", "ref", "reference",
})
# Concepts a subject-named column may express: counts and amounts, never money or rates.
SUBJECT_HEAD_CLASSES = frozenset({"value", "quantity", "population", "production"})

STOPWORDS = frozenset({
    "the", "a", "an", "of", "and", "or", "in", "on", "for", "to", "from", "by", "with",
    "over", "across", "between", "during", "per", "at", "as", "is", "are", "was", "were",
    "be", "been", "it", "its", "this", "that", "these", "those", "what", "which", "how",
    "much", "many", "me", "my", "i", "we", "our", "you", "your", "all", "each", "every",
    "find", "get", "show", "list", "give", "collect", "compile", "extract", "scrape",
    "fetch", "download", "retrieve", "gather", "obtain", "pull", "grab", "browse", "explore",
    "lookup", "search", "provide", "return", "display", "view",
    "research", "track", "tracking", "compare", "comparison", "want", "need", "please",
    "data", "dataset", "datasets", "statistic", "statistics", "stats", "figure", "figures",
    "number", "numbers", "trend", "trends", "history", "historical", "information", "info",
    "table", "tables", "chart", "charts", "report", "reports", "overview", "summary",
    "latest", "current", "recent", "recently", "around", "about", "last", "past",
    "previous", "prior", "next", "year", "years", "month", "months", "decade", "decades",
    "quarter", "quarters", "week", "weeks", "day", "days", "since", "until", "through",
    "annual", "yearly", "monthly", "time", "series", "timeline", "over", "evolution",
    "globally", "worldwide", "breakdown", "by", "vs", "versus", "across", "wise",
})

GENERIC_FIELD_NAMES = re.compile(r"^(?:column|col|field|value|values|text|label|item)(?:_?\d+)?$")

_MONEY = re.compile(r"(?:[$€£¥₹₩₽]|\b(?:usd|eur|gbp|inr|jpy|cny|rs\.?|us\$)\b)\s?-?\d|\d\s?(?:usd|eur|gbp|inr|jpy|cny|dollars?|euros?|rupees?)\b", re.I)
_PERCENT = re.compile(r"-?\d[\d.,]*\s?(?:%|percent\b|pct\b|pp\b)", re.I)
_BOUND = re.compile(r"\b(?:under|over|below|less than|more than|up to|at least|at most)\s+(?:[$€£¥₹]|\d)", re.I)
_BARE_QUANTITY = re.compile(r"[~≈<>+-]?\s?\d[\d,]*(?:\.\d+)?(?:\s?[^\s\d]{1,12}){0,2}", re.I)
_FORMATTED_NUMBER = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?!\d)|(?<![\w.])\d+\.\d+(?!\d)")


# ----------------------------------------------------------------------------
# Token utilities
# ----------------------------------------------------------------------------

def _singular(token: str) -> str:
    if len(token) <= 3 or token.endswith(("ss", "us", "is")):
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith(("ches", "shes", "xes", "sses")):
        return token[:-2]
    if token.endswith("s"):
        return token[:-1]
    return token


@lru_cache(maxsize=8192)
def tokens(value: str) -> tuple[str, ...]:
    """Split names such as ``averagePackPrice_USD`` into ('average','pack','price','usd')."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(value or ""))
    parts = re.findall(r"[a-z]+|\d+", text.casefold())
    return tuple(_singular(part) for part in parts if part)


def _typo_of(token: str) -> str | None:
    """Lexicon word ``token`` is a likely misspelling of, if any.

    Typos almost always keep the first letter ("yeild" -> yield); a different
    first letter ("heading" vs "reading") is a different word.
    """
    if len(token) < 5:
        return None
    # Same first letter and near-equal length: "yeild" -> yield, but neither
    # "heading" -> reading nor "charging" -> charge (a different word form).
    candidates = [word for word in _VOCABULARY if word[0] == token[0] and abs(len(word) - len(token)) <= 1]
    match = get_close_matches(token, candidates, n=1, cutoff=0.8)
    return match[0] if match else None


@lru_cache(maxsize=8192)
def token_class(token: str) -> str | None:
    """Lexicon class of a token, tolerating spelling mistakes ("yeild" -> yield)."""
    if token in _MEMBER_TO_CLASS:
        return _MEMBER_TO_CLASS[token]
    typo = _typo_of(token)
    return _MEMBER_TO_CLASS[typo] if typo else None


@lru_cache(maxsize=8192)
def correct_token(token: str) -> str:
    """Return the lexicon spelling for a misspelt generic term, else the token."""
    if token in _MEMBER_TO_CLASS:
        return token
    return _typo_of(token) or token


def similar(first: str, second: str) -> float:
    """Lexical similarity for single tokens (stems, typos, derivations)."""
    if first == second:
        return 1.0
    shorter, longer = sorted((first, second), key=len)
    if len(shorter) >= 5 and longer.startswith(shorter[:max(5, int(len(shorter) * 0.8))]):
        return 0.85  # agriculture ~ agricultural, electrify ~ electric
    if len(shorter) >= 4:
        return SequenceMatcher(None, first, second).ratio()
    return 0.0


def content_tokens(value: str) -> list[str]:
    return [token for token in tokens(value) if token not in STOPWORDS and not token.isdigit()]


def compact_tokens(text: str) -> set[str]:
    """Content tokens plus adjacent pairs joined: "eExample" is the tokens e and
    example (camelCase) and also "example", so a name written as one word
    matches however the page happens to spell it."""
    words = content_tokens(text)
    return set(words) | {first + second for first, second in zip(words, words[1:])}


# Parts of a web address that say nothing about a topic: every ".com" page is not "about" the word com.
ADDRESS_WORDS = frozenset({
    "www", "http", "https", "com", "org", "net", "gov", "edu", "int", "mil", "co", "ac", "io", "in", "uk", "us",
    "html", "htm", "php", "aspx", "asp", "jsp", "index", "default", "en",
})


def topic_tokens(text: str) -> set[str]:
    """Like ``compact_tokens`` but without address parts, for matching a request to pages and sites.

    "example.com" is about "example", not "com": otherwise every .com result would look on topic.
    """
    words = [word for word in content_tokens(text) if word not in ADDRESS_WORDS]
    return set(words) | {first + second for first, second in zip(words, words[1:])}


def humanize(name: str) -> str:
    return " ".join(tokens(name)) or name


def head_token(name: str) -> str | None:
    """The word a field actually measures: "average_pack_price" -> price.

    Words after "per" are a rate's denominator ("cost_per_unit" -> cost).
    """
    parts = list(tokens(name))
    if "per" in parts[1:]:
        parts = parts[:parts.index("per")]
    meaningful = [token for token in parts
                  if token not in MODIFIERS and token not in UNIT_TOKENS and not token.isdigit()]
    return meaningful[-1] if meaningful else None


def unit_hints(name: str) -> list[str]:
    return [token for token in tokens(name) if token in UNIT_TOKENS]


# ----------------------------------------------------------------------------
# Value typing
# ----------------------------------------------------------------------------

def value_kind(value: object) -> str:
    """Classify a cell: money | percent | period | number | text | empty."""
    text = str(value or "").strip()
    if not text:
        return "empty"
    if parse_period(text):
        return "period"
    # Prose that merely mentions an amount ("Cars under $20,000 are harder to
    # find") is text: a value cell is short and mostly quantity.
    if len(re.findall(r"[a-z]", text, re.I)) > 18:
        return "text"
    # "Best cars under $30,000" states a bound, not a value.
    if _BOUND.search(text):
        return "text"
    if _MONEY.search(text):
        return "money"
    if _PERCENT.search(text):
        return "percent"
    # A number with at most a short unit ("300 mi", "4.1 t/ha", "1.2 million"),
    # or a formatted figure inside short text ("about 9,870 units"). Labels
    # such as "Figure 1" or "Model 3" stay text.
    if _BARE_QUANTITY.fullmatch(text):
        return "number"
    if _FORMATTED_NUMBER.search(text) and len(re.findall(r"[a-z]", text, re.I)) <= 25:
        return "number"
    return "text"


QUANTITY_KINDS = frozenset({"money", "percent", "number"})


def has_quantity(value: object) -> bool:
    return value_kind(value) in QUANTITY_KINDS


def compatible(concept: "ConceptSpec", kind: str, strict: bool = False) -> bool:
    """Whether a value of ``kind`` can express the concept (money is not a rate).

    ``strict`` is used when the only evidence is prose: a bare number next to
    the word "prices" is usually a count, so a monetary concept then needs a
    currency amount and a rate needs a percentage.
    """
    if kind not in QUANTITY_KINDS:
        return False
    if concept.monetary:
        return kind == "money" or (kind == "number" and not strict)
    if concept.lexicon_class in {"rate", "change"}:
        return kind == "percent" or (kind == "number" and not strict)
    return True


# ----------------------------------------------------------------------------
# Concepts requested by a plan and their matching against observations
# ----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ConceptSpec:
    name: str
    kind: str                      # measure | time | dimension | entity | topic
    lexicon_class: str | None
    aliases: tuple[tuple[str, ...], ...] = ()

    @property
    def monetary(self) -> bool:
        return self.lexicon_class in MONETARY

    @property
    def is_measure(self) -> bool:
        return self.kind == "measure"


@dataclass(frozen=True, slots=True)
class ConceptMatch:
    concept: str
    field: str
    credit: float
    via: str  # exact | alias | synonym | fuzzy | related | value_type | text | learned

    def as_dict(self) -> dict[str, object]:
        return {"field": self.field, "credit": round(self.credit, 3), "via": self.via}


GENERIC_CLASSES = frozenset({"value", "quantity"})


def concept_spec(name: str, kind: str | None = None, aliases: Iterable[str] = ()) -> ConceptSpec:
    """Build a concept from a requested name such as "price_usd" or "yield_quantity".

    The most specific lexicon class wins ("yield_quantity" -> yield, not the
    generic quantity); names outside the lexicon keep their head word.
    """
    parts = [correct_token(token) for token in tokens(name)
             if token not in UNIT_TOKENS and token not in MODIFIERS and not token.isdigit()]
    if not parts:
        parts = [name.casefold()]
    classified = [(token, token_class(token)) for token in reversed(parts)]
    specific = next((item for item in classified if item[1] and item[1] not in GENERIC_CLASSES), None)
    generic = next((item for item in classified if item[1]), None)
    head, lexicon_class = specific or generic or (parts[-1], None)
    if kind is None:
        kind = LEXICON[lexicon_class][0] if lexicon_class else "measure"
    alias_tokens = {tuple(tokens(alias)) for alias in aliases if tokens(alias)}
    alias_tokens.add(tuple(tokens(name)))
    return ConceptSpec(name=lexicon_class or head, kind=kind, lexicon_class=lexicon_class,
                       aliases=tuple(sorted(alias_tokens)))


def _contains(sequence: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    size = len(phrase)
    return size > 0 and any(sequence[index:index + size] == phrase for index in range(len(sequence) - size + 1))


def _name_credit(field_name: str, concept: ConceptSpec) -> tuple[float, str]:
    field_tokens = tokens(field_name)
    if not field_tokens:
        return 0.0, ""
    head = head_token(field_name)
    best, via = 0.0, ""
    for alias in concept.aliases:
        if len(alias) > 1 and _contains(field_tokens, alias):
            best, via = 0.97, "alias"
    for token in field_tokens:
        if token in MODIFIERS or token in UNIT_TOKENS or token.isdigit():
            continue
        weight = 1.0 if token == head else 0.88
        if token == concept.name or (token,) in concept.aliases:
            credit, kind = 1.0, "exact" if token == concept.name else "alias"
        else:
            token_cls = token_class(token)
            if concept.lexicon_class and token_cls == concept.lexicon_class:
                credit, kind = 0.9, "synonym"
            elif similar(token, concept.name) >= 0.85:
                credit, kind = 0.8, "fuzzy"
            elif concept.lexicon_class and token_cls and \
                    frozenset({token_cls, concept.lexicon_class}) in RELATED:
                credit, kind = RELATED[frozenset({token_cls, concept.lexicon_class})], "related"
            else:
                continue
        if credit * weight > best:
            best, via = credit * weight, kind
    return best, via


class ConceptMatcher:
    """Score how well observation fields express a set of requested concepts.

    Results are memoised per (field name, concept) because the same headers
    repeat across every row of a table and across runs.
    """

    def __init__(self, concepts: Iterable[ConceptSpec], subject_heads: Iterable[str] = ()) -> None:
        self.concepts = list(concepts)
        self._name_cache: dict[tuple[str, str], tuple[float, str]] = {}
        self._learned: dict[str, str] = {}
        # Nouns of the request ("hospitals", "wins"): a numeric column named after
        # one is the quantity the user asked for, even without a measure word.
        self.subject_heads = frozenset(
            head for head in (correct_token(token) for value in subject_heads for token in tokens(value)[-1:])
            if head not in IDENTIFIER_HEADS)

    def names_subject(self, field_name: str) -> bool:
        head = head_token(field_name)
        if not head or head in IDENTIFIER_HEADS or not self.subject_heads:
            return False
        return any(similar(head, subject) >= 0.85 for subject in self.subject_heads)

    def learn(self, mapping: Mapping[str, str | None]) -> None:
        """Record externally resolved header->concept mappings (e.g. from a model)."""
        for header, concept in mapping.items():
            if concept:
                self._learned[header] = concept

    def name_credit(self, field_name: str, concept: ConceptSpec) -> tuple[float, str]:
        key = (field_name, concept.name)
        if key not in self._name_cache:
            credit, via = _name_credit(field_name, concept)
            if credit < 0.75 and self._learned.get(field_name) == concept.name:
                credit, via = 0.78, "learned"
            self._name_cache[key] = (credit, via)
        return self._name_cache[key]

    def match(self, fields: Mapping[str, str], concept: ConceptSpec,
              exclude: Iterable[str] = (), context: str = "") -> ConceptMatch | None:
        """Best evidence that ``fields`` express ``concept``.

        ``context`` is the caption or heading the row was found under; it gives
        generic columns ("value", "column_2") their meaning.
        """
        excluded = set(exclude)
        best: ConceptMatch | None = None

        def consider(candidate: ConceptMatch) -> None:
            nonlocal best
            if best is None or candidate.credit > best.credit:
                best = candidate

        kinds = {name: value_kind(value) for name, value in fields.items() if name not in excluded}
        def names_other_measure(name: str) -> bool:
            if GENERIC_FIELD_NAMES.match(name):
                return False
            named = token_class(head_token(name) or "")
            return bool(named) and named in MEASURE_CLASSES and named not in {"value", concept.lexicon_class}

        text_targets = [name for name, kind in kinds.items()
                        if compatible(concept, kind, strict=True) and not names_other_measure(name)]
        for name, value in fields.items():
            if name in excluded:
                continue
            kind = kinds[name]
            generic = bool(GENERIC_FIELD_NAMES.match(name))
            # A generic header says nothing about what it measures.
            credit, via = (0.0, "") if generic else self.name_credit(name, concept)
            if concept.is_measure and credit:
                # A related or generic header ("value", "amount") is weak evidence,
                # so its cell must carry the concept's own kind of quantity.
                # So is a concept word buried inside a long header ("top 20 cars with
                # the largest price increases"): only a head-noun match is strong.
                weak = via == "related" or credit < 0.9
                if kind in QUANTITY_KINDS and not compatible(concept, kind, strict=weak):
                    credit *= 0.25  # "price" header over a percentage column
                elif kind not in QUANTITY_KINDS:
                    credit *= 0.3   # a measure header over text is weak evidence
            if credit:
                consider(ConceptMatch(concept.name, name, credit, via))
            if not concept.is_measure:
                continue
            # A header naming a different measure ("change", "rate") overrides
            # what the cell's unit suggests: dollars under "change" are not a price.
            if names_other_measure(name):
                continue
            if concept.monetary and kind == "money":
                consider(ConceptMatch(concept.name, name, 0.62, "value_type"))
            elif concept.lexicon_class in {"rate", "change"} and kind == "percent":
                consider(ConceptMatch(concept.name, name, 0.62, "value_type"))
            elif compatible(concept, kind) and (generic or token_class(head_token(name) or "") == "value"):
                if concept.name == "value":
                    # The plan asked for any quantity about the subject.
                    consider(ConceptMatch(concept.name, name, 0.6, "value_type"))
                elif context and self._mentions(context, concept):
                    consider(ConceptMatch(concept.name, name, 0.65, "context"))
            elif kind in {"number", "percent"} and concept.lexicon_class in SUBJECT_HEAD_CLASSES \
                    and self.names_subject(name):
                # "Hospitals: 1,280" for "hospitals per state": the column is named
                # after what the user counts.
                consider(ConceptMatch(concept.name, name, 0.6, "subject"))
            # Prose naming the concept ("prices fell to $108/kWh") supports the
            # row's compatible quantity; the value stays the quantity, not the prose.
            if kind == "text":
                targets = [field for field in text_targets if field != name]
                if targets and self._mentions(value, concept):
                    consider(ConceptMatch(concept.name, targets[0], 0.7, "text"))
        return best

    @staticmethod
    def _mentions(text: str, concept: ConceptSpec) -> bool:
        text_tokens = tokens(text)
        if any(_contains(text_tokens, alias) for alias in concept.aliases):
            return True
        return bool(concept.lexicon_class) and any(
            token_class(token) == concept.lexicon_class for token in text_tokens)

    def unresolved_headers(self, rows: Iterable[Mapping[str, str]]) -> list[str]:
        """Headers that carry quantities but map to no requested concept."""
        headers: dict[str, None] = {}
        for fields in rows:
            for name, value in fields.items():
                if name in self._learned or GENERIC_FIELD_NAMES.match(name) or not has_quantity(value):
                    continue
                if all(self.name_credit(name, concept)[0] < 0.5 for concept in self.concepts):
                    headers[name] = None
        return list(headers)


def time_field_names(fields: Mapping[str, str]) -> list[str]:
    """Fields that state an observation's period, by name or by every value."""
    names = []
    for name, value in fields.items():
        name_tokens = tokens(name)
        if any(token_class(token) == "period" for token in name_tokens if token not in MODIFIERS) and \
                not any(token in {"updated", "modified", "published", "created"} for token in name_tokens):
            names.append(name)
        elif GENERIC_FIELD_NAMES.match(name) and parse_period(value):
            names.append(name)
        elif name in {"period"} and value:
            names.append(name)
    return names


def canonical(value: str) -> set[str]:
    """Canonical concept names mentioned in a string (used for plan grounding)."""
    result: set[str] = set()
    for token in content_tokens(value):
        corrected = correct_token(token)
        result.add(token_class(corrected) or corrected)
    return result


def ordered_concepts(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        for token in content_tokens(value):
            corrected = correct_token(token)
            concept = token_class(corrected) or corrected
            if concept not in result:
                result.append(concept)
    return result
