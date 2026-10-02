"""Graph-ready views of a dataset: one chart per numeric parameter.

A *parameter* is any column that holds numbers (price, wins, rainfall,
population...). Each becomes its own chart, so values with different units
never share an axis:

* ``line`` when its rows cover at least two periods, one series per value of
  the best grouping column (team, city, model...), or per ``series``;
* ``bar`` otherwise (for example population by city), against the grouping
  column with the most distinct values.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Iterable

from vora.extraction.numbers import CURRENCY as _CURRENCY
from vora.extraction.numbers import NUMBER as _NUMBER
from vora.extraction.numbers import to_number
from vora.extraction.semantics import IDENTIFIER_HEADS, QUANTITY_KINDS, head_token, unit_hints, value_kind
from vora.shared.contracts import Observation

from vora.output.tables import values

MAX_SERIES = 8
MAX_BARS = 30
RESERVED = {"period", "series", "statement", "text", "context", "url", "link", "href", "image"}

def _identifier(name: str) -> bool:
    """Columns that label rows ("rank", "hospital_id", "purposeid") are not measures or groups."""
    lowered = name.casefold()
    return head_token(name) in IDENTIFIER_HEADS or lowered.endswith(("id", "_id", "code", "_no")) or lowered == "no"


def _is_quantity(value: str) -> bool:
    return value_kind(value) in QUANTITY_KINDS and to_number(value) is not None


def _unit(name: str, cells: list[str]) -> str:
    """The unit most cells are written in ("%", "₹", "mm"...), if any."""
    found: Counter[str] = Counter()
    for cell in cells:
        if "%" in cell:
            found["%"] += 1
        elif currency := _CURRENCY.search(cell):
            symbol = currency.group(0).strip().rstrip(".")
            found[symbol.upper() if symbol.isalpha() else symbol] += 1
        else:
            match = _NUMBER.search(cell)
            tail = cell[match.end():].strip().split(" ")[0].casefold() if match else ""
            if tail and tail.isalpha() and len(tail) <= 8:
                found[tail] += 1
    if found:
        unit, count = found.most_common(1)[0]
        if count * 2 >= len(cells):
            return unit
    hints = unit_hints(name)
    return hints[-1] if hints else ""


def _rows(records: Iterable[Observation]) -> list[tuple[Observation, dict[str, str]]]:
    return [(record, values(record)) for record in records]


def _dimensions(rows: list[tuple[Observation, dict[str, str]]], numeric: set[str]) -> dict[str, int]:
    """Text columns usable for grouping, with their number of distinct values."""
    distinct: dict[str, set[str]] = {}
    for _, cells in rows:
        for name, value in cells.items():
            if name in RESERVED or name in numeric or not value or _identifier(name):
                continue
            distinct.setdefault(name, set()).add(value)
    return {name: len(seen) for name, seen in distinct.items() if len(seen) >= 2}


# A column is a numeric parameter only when most of its values are numbers: a text column where a few
# cells happen to contain figures ("notification under section 20 of 2026") is not.
NUMERIC_SHARE = 0.6


def _numeric_columns(rows: list[tuple[Observation, dict[str, str]]]) -> dict[str, list[str]]:
    cells: dict[str, list[str]] = {}
    filled: dict[str, int] = {}
    for _, row in rows:
        for name, value in row.items():
            if name in RESERVED or not value or _identifier(name):
                continue
            filled[name] = filled.get(name, 0) + 1
            if _is_quantity(value):
                cells.setdefault(name, []).append(value)
    return {name: found for name, found in cells.items()
            if len(found) >= 2 and len(found) >= NUMERIC_SHARE * filled[name]}


def graph_parameters(records: list[Observation]) -> dict[str, Any]:
    """Every numeric parameter in the dataset, with what a chart of it would show."""
    rows = _rows(records)
    numeric = _numeric_columns(rows)
    dimensions = _dimensions(rows, set(numeric))
    parameters = []
    for name, cells in numeric.items():
        periods = sorted({row.get("period", "") for _, row in rows if row.get(name) and row.get("period")})
        parameters.append({
            "name": name, "unit": _unit(name, cells), "points": len(cells),
            "x_kind": "period" if len(periods) >= 2 else "category",
            "period_min": periods[0] if periods else None, "period_max": periods[-1] if periods else None,
            "series_count": len({row.get("series", "") for _, row in rows if row.get(name)}),
        })
    parameters.sort(key=lambda item: -item["points"])
    return {"parameters": parameters,
            "dimensions": [{"name": name, "distinct": count} for name, count in
                           sorted(dimensions.items(), key=lambda item: -item[1])],
            "row_count": len(records)}


def _in_range(period: str, start: str | None, end: str | None) -> bool:
    if not period:
        return not (start or end)
    if start and period[:len(start)] < start:
        return False
    return not (end and period[:len(end)] > end)


def _group_column(rows: list[tuple[Observation, dict[str, str]]], name: str, dimensions: dict[str, int],
                  limit: int) -> str | None:
    """The grouping column for a parameter's series or bars."""
    relevant = [cells for _, cells in rows if cells.get(name)]
    usable = {}
    for column in dimensions:
        seen = {cells.get(column) for cells in relevant if cells.get(column)}
        if 2 <= len(seen) <= limit:
            usable[column] = len(seen)
    return max(usable, key=usable.get) if usable else None


def _point(record: Observation, x: str, value: float, raw: str) -> dict[str, Any]:
    return {"x": x, "value": value, "raw": raw, "observation_id": record.id, "source_url": record.source_url}


def graph_data(records: list[Observation], parameters: list[str] | None = None, *,
               start: str | None = None, end: str | None = None,
               series: list[str] | None = None) -> dict[str, Any]:
    """One chart per requested parameter (all numeric parameters by default)."""
    available = graph_parameters(records)
    names = [item["name"] for item in available["parameters"]]
    selected = [name for name in (parameters or names) if name in names]
    rows = [(record, cells) for record, cells in _rows(records)
            if _in_range(cells.get("period", ""), start, end)]
    numeric = set(_numeric_columns(rows))
    dimensions = _dimensions(rows, numeric)
    info = {item["name"]: item for item in available["parameters"]}
    charts = []
    for name in selected:
        mine = [(record, cells) for record, cells in rows if cells.get(name) and to_number(cells[name]) is not None]
        periods = {cells.get("period") for _, cells in mine if cells.get("period")}
        chart: dict[str, Any] = {"parameter": name, "unit": info[name]["unit"], "omitted": 0, "duplicates": 0}
        if len(periods) >= 2:
            group = _group_column(mine, name, dimensions, MAX_SERIES * 3)
            groups: dict[str, dict[str, dict[str, Any]]] = {}
            for record, cells in mine:
                key = (cells.get(group) if group else None) or cells.get("series") or name
                if series and key not in series:
                    continue
                period = cells.get("period")
                if not period:
                    continue
                points = groups.setdefault(key, {})
                if period in points:
                    chart["duplicates"] += 1
                    continue
                points[period] = _point(record, period, to_number(cells[name]), cells[name])
            ranked = sorted(groups.items(), key=lambda item: -len(item[1]))
            kept = ranked[:MAX_SERIES]
            chart.update(type="line", x="period", group=group, omitted=len(ranked) - len(kept), series=[
                {"name": key, "points": sorted(points.values(), key=lambda point: point["x"])}
                for key, points in kept])
        else:
            group = _group_column(mine, name, dimensions, 10_000)
            bars: dict[str, dict[str, Any]] = {}
            for record, cells in mine:
                if group and not cells.get(group):
                    chart["omitted"] += 1  # e.g. a sentence with no city next to a table of cities
                    continue
                label = (cells.get(group) if group else None) or cells.get("series") or record.source_domain or name
                if series and label not in series:
                    continue
                if label in bars:
                    chart["duplicates"] += 1
                    continue
                bars[label] = _point(record, label, to_number(cells[name]), cells[name])
            ordered = sorted(bars.values(), key=lambda point: -point["value"])
            chart.update(type="bar", x=group or "label", group=group, omitted=chart["omitted"] + max(0, len(ordered) - MAX_BARS),
                         series=[{"name": name, "points": ordered[:MAX_BARS]}])
        charts.append(chart)
    return {"charts": charts, "parameters": names, "selected": selected, "row_count": len(records)}
