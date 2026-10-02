"""Judge page blocks as a whole, not only their rows one by one.

A page is made of blocks: tables, groups of repeated cards, the tables of
each tab or further page. Rows are scored individually, but a block that as
a whole does not carry the requested data (a "related items" table where one
row happens to pass) should not contribute accepted rows.

*Support* of a block is the mean of three signals over its rows:

* evidence: how strongly rows express the requested measure (``coverage``)
* relevance: how well rows match the subject
* consistency: whether the measured values share one kind (all money, all
  percentages...), as a real data column does

Rows of blocks with support below ``MIN_SUPPORT`` are demoted to partial,
kept for review, never deleted.

The same numbers often reach a page twice (a table and the chart data behind
it, or two tabs showing the same year). Overlapping blocks are resolved like
non-maximum suppression: when most of a block's values (period, series,
number) already appear in a stronger block of the same site, its repeated
rows are demoted.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from vora.shared.contracts import Observation

from vora.extraction.numbers import to_number
from vora.extraction.semantics import value_kind

MIN_SUPPORT = 0.30
OVERLAP = 0.6


def _key(item: Observation) -> tuple[str, str] | None:
    return (item.source_url, item.block_id) if item.block_id else None


def _measured(item: Observation) -> str:
    """The value that answered the request (first matched required concept)."""
    for match in item.concept_matches.values():
        if match.get("via") == "record":
            continue  # the record's title names it; it is not a measured value
        field = match.get("field")
        if field and field in item.fields:
            return item.fields[field]
    return ""


def block_support(rows: list[Observation]) -> float:
    """Mean of evidence, relevance and value-kind consistency over a block."""
    if not rows:
        return 0.0
    evidence = sum(item.score_breakdown.get("coverage", 0.0) for item in rows) / len(rows)
    relevance = sum(item.relevance_score for item in rows) / len(rows)
    # Share of the block's rows whose measured value has the block's usual kind:
    # rows with no measured value at all count against it.
    kinds = Counter(value_kind(_measured(item)) for item in rows if _measured(item))
    consistency = kinds.most_common(1)[0][1] / len(rows) if kinds else 0.0
    # Well-formed records have no measured value; being records is their consistency.
    records = sum(1 for item in rows if "record" in item.concept_matches)
    consistency = max(consistency, records / len(rows))
    return round((evidence + relevance + consistency) / 3, 3)


def _signature(item: Observation) -> tuple[str, str, float] | None:
    number = to_number(_measured(item))
    if number is None:
        return None
    return (item.normalized.get("period", ""), item.normalized.get("series", "").casefold(), round(number, 6))


def _demote(item: Observation, reason: str) -> Observation:
    return item.model_copy(update={"status": "partial", "tier": "partial", "reasons": [*item.reasons, reason]})


def apply_block_support(accepted: list[Observation], partial: list[Observation],
                        rejected: list[Observation]) -> tuple[list[Observation], list[Observation], list[Observation]]:
    """Demote accepted rows of unsupported or redundant blocks to partial."""
    blocks: dict[tuple[str, str], list[Observation]] = defaultdict(list)
    for item in [*accepted, *partial, *rejected]:
        if (key := _key(item)) is not None:
            blocks[key].append(item)
    if not blocks:
        return accepted, partial, rejected
    support = {key: block_support(rows) for key, rows in blocks.items()}

    demoted: dict[str, str] = {}
    for item in accepted:
        key = _key(item)
        if key is not None and support[key] < MIN_SUPPORT:
            demoted[item.id] = f"Block doesn't support the requested data (support {support[key]:.2f})"

    # Overlapping blocks of one site: the strongest keeps the shared values.
    by_site: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for key in blocks:
        by_site[blocks[key][0].source_domain or key[0]].append(key)
    for keys in by_site.values():
        kept_values: set[tuple[str, str, float]] = set()
        for key in sorted(keys, key=lambda item: -support[item]):
            rows = [item for item in blocks[key] if item.status == "accepted" and item.id not in demoted]
            values = {signature: item for item in rows if (signature := _signature(item)) is not None}
            if values and kept_values:
                repeated = [signature for signature in values if signature in kept_values]
                if len(repeated) / len(values) >= OVERLAP:
                    for signature in repeated:
                        demoted[values[signature].id] = "Same values as a stronger block on this site"
            kept_values.update(values)

    if not demoted:
        return accepted, partial, rejected
    moved = [_demote(item, demoted[item.id]) for item in accepted if item.id in demoted]
    return [item for item in accepted if item.id not in demoted], [*moved, *partial], rejected
