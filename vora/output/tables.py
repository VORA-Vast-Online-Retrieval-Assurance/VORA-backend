"""Turn scored observations into the table the API, live stream and graphs share."""

from __future__ import annotations

from typing import Any

from vora.shared.contracts import GoalPlan, Observation


def values(item: Observation) -> dict[str, str]:
    """Normalized values when scoring produced them, otherwise the raw fields."""
    return item.normalized or item.fields


def columns_for(records: list[Observation], plan: GoalPlan | None) -> list[str]:
    """Period and series first, then the plan's measures, then everything else."""
    preferred = ["period", "series"]
    if plan is not None:
        preferred += [item.name for item in plan.required_concepts if item.kind != "time"]
    seen = list(dict.fromkeys(key for record in records for key in values(record)))
    return [name for name in preferred if name in seen] + [name for name in seen if name not in preferred]


def build_table(records: list[Observation], plan: GoalPlan | None,
                columns: list[str] | None = None) -> tuple[list[str], list[list[str]]]:
    columns = columns if columns is not None else columns_for(records, plan)
    rows = [[values(record).get(column, "") for column in columns] for record in records]
    return columns, rows


def record_meta(item: Observation) -> dict[str, Any]:
    """Per-row provenance and interpretation, kept apart from the values."""
    return {
        "id": item.id, "status": item.status, "tier": item.tier, "score": item.score,
        "score_breakdown": item.score_breakdown, "reasons": item.reasons,
        "concept_matches": item.concept_matches, "fields": item.fields,
        "data_period": item.data_period, "period_start": item.period_start,
        "period_end": item.period_end, "period_granularity": item.period_granularity,
        "time_basis": item.time_basis, "time_inferred": item.time_inferred,
        "temporal_confidence": item.temporal_confidence,
        "source_url": item.source_url, "source_domain": item.source_domain,
        "source_title": item.source_title, "published_at": item.published_at,
        "modified_at": item.modified_at, "fetched_at": item.fetched_at,
        "method": item.method, "extraction_confidence": item.extraction_confidence,
        "context": item.context, "content_role": item.content_role, "block_id": item.block_id,
    }
