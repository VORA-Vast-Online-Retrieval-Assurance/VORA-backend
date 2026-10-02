"""Deterministic temporal reasoning shared by planning, extraction and scoring.

Every date calculation in VORA goes through this module so that the planner,
the extractor and the scorer can never disagree about what a period means.
Language models are never asked to do date arithmetic.

Relative-range policy (the single definition used everywhere):

* ``last/past/previous N years`` -> the N calendar years ending with the current
  calendar year, inclusive. On 2026-09-29, "last 10 years" is 2017..2026, which
  is exactly 10 periods. Adding "complete" or "full" ("last 5 full years") ends
  the window at the previous calendar year instead.
* ``last N months`` / ``last N quarters`` -> N calendar months or quarters ending
  with the current one, inclusive.
* ``last N weeks`` / ``last N days`` -> a rolling window of N*7 or N days ending
  today, inclusive.
* ``last decade`` -> 10 years under the year rule above.
* ``since YYYY`` -> YYYY through the current year.
* ``last year`` / ``previous year`` -> the previous calendar year only.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Iterable, Mapping

MONTHS = {name.lower(): index for index, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): index for index, name in enumerate(calendar.month_abbr) if name})
MONTHS["sept"] = 9
_MONTH_PATTERN = "|".join(sorted(MONTHS, key=len, reverse=True))

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fifteen": 15, "twenty": 20, "thirty": 30,
}

# A year that is not part of a larger number or a monetary amount ("$2,024").
_YEAR = r"(?<![\d$€£¥₹.,/])((?:19|20)\d{2})(?![\d])(?!,\d)"


def today_utc() -> date:
    return datetime.now(UTC).date()


@dataclass(frozen=True, slots=True)
class Period:
    """A concrete calendar interval with a stable, sortable label."""

    label: str
    start: date
    end: date
    granularity: str  # year | half | quarter | month | day | range

    def overlaps(self, start: date, end: date) -> bool:
        return self.start <= end and self.end >= start

    def within(self, start: date, end: date) -> bool:
        return self.start >= start and self.end <= end


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """A resolved request-level time constraint."""

    expression: str
    start: date
    end: date
    granularity: str
    relative: bool
    policy: str
    periods: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class TemporalResolution:
    period: Period | None
    basis: str
    inferred: bool
    confidence: float
    note: str = ""


# ----------------------------------------------------------------------------
# Period construction helpers
# ----------------------------------------------------------------------------

def year_period(year: int) -> Period:
    return Period(str(year), date(year, 1, 1), date(year, 12, 31), "year")


def month_period(year: int, month: int) -> Period:
    last = calendar.monthrange(year, month)[1]
    return Period(f"{year}-{month:02d}", date(year, month, 1), date(year, month, last), "month")


def quarter_period(year: int, quarter: int) -> Period:
    first = 3 * (quarter - 1) + 1
    last_month = first + 2
    return Period(f"{year}-Q{quarter}", date(year, first, 1),
                  date(year, last_month, calendar.monthrange(year, last_month)[1]), "quarter")


def day_period(value: date) -> Period:
    return Period(value.isoformat(), value, value, "day")


def range_period(start_year: int, end_year: int, label: str | None = None) -> Period:
    start_year, end_year = sorted((start_year, end_year))
    return Period(label or f"{start_year}–{end_year}", date(start_year, 1, 1),
                  date(end_year, 12, 31), "range")


def _valid_year(value: int, today: date | None = None) -> bool:
    ceiling = (today or today_utc()).year + 30
    return 1900 <= value <= ceiling


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# Parsing periods from cells, headers and free text
# ----------------------------------------------------------------------------

_CELL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.I), kind) for pattern, kind in (
        (r"^(\d{4})-(\d{2})-(\d{2})(?:[t\s].*)?$", "iso_day"),
        # No "." separator: "6.2031" and "2024.5" are decimals, not months.
        (r"^(\d{4})[-/](\d{1,2})$", "year_month"),
        (r"^(\d{1,2})[-/_ ](\d{4})$", "month_year"),
        (rf"^({_MONTH_PATTERN})\.?[\s,_-]+(\d{{4}})$", "name_month_year"),
        (rf"^(\d{{4}})[\s,_-]+({_MONTH_PATTERN})\.?$", "year_name_month"),
        (r"^q([1-4])[\s_-]*(?:fy)?[\s_-]*(\d{4})$", "quarter_first"),
        (r"^(\d{4})[\s_-]*q([1-4])$", "quarter_last"),
        (r"^([1-4])q[\s_-]*(\d{4})$", "quarter_first"),
        (r"^h([12])[\s_-]*(\d{4})$", "half_first"),
        (r"^(\d{4})[\s_-]*h([12])$", "half_last"),
        (r"^fy[\s_-]*(\d{4})$", "fiscal_year"),
        (r"^fy[\s_-]*(\d{2})$", "fiscal_short"),
        (r"^(\d{4})\s*[-–/]\s*(\d{2})$", "split_year"),
        (r"^(\d{4})\s*(?:-|–|—|to|through|until)\s*(\d{4})$", "year_range"),
        (r"^(?:cy|year)?[\s_-]*(\d{4})[a-z*†]?$", "year"),
        (rf"^(\d{{1,2}})(?:st|nd|rd|th)?[\s_-]+({_MONTH_PATTERN})\.?,?[\s_-]+(\d{{4}})$", "day_name_month_year"),
        (rf"^({_MONTH_PATTERN})\.?[\s_-]+(\d{{1,2}})(?:st|nd|rd|th)?,?[\s_-]+(\d{{4}})$", "name_month_day_year"),
        (r"^(\d{1,2})[/.](\d{1,2})[/.](\d{4})$", "numeric_day"),
    )
)


def parse_period(value: object, today: date | None = None) -> Period | None:
    """Parse a short value (a cell, header or field) that *is* a period."""
    text = re.sub(r"\s+", " ", str(value or "")).strip().strip("()[]").strip()
    if not text or len(text) > 40:
        return None
    text = text.replace("–", "–").replace("—", "—")
    for pattern, kind in _CELL_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        period = _build(kind, match.groups(), today)
        if period is not None:
            return period
    return None


def _build(kind: str, groups: tuple[str, ...], today: date | None) -> Period | None:
    def year_ok(value: int) -> bool:
        return _valid_year(value, today)

    if kind == "iso_day":
        year, month, day = map(int, groups)
        value = _safe_date(year, month, day)
        return day_period(value) if value and year_ok(year) else None
    if kind == "year_month":
        year, month = int(groups[0]), int(groups[1])
        return month_period(year, month) if 1 <= month <= 12 and year_ok(year) else None
    if kind == "month_year":
        month, year = int(groups[0]), int(groups[1])
        return month_period(year, month) if 1 <= month <= 12 and year_ok(year) else None
    if kind == "name_month_year":
        month, year = MONTHS[groups[0].lower()], int(groups[1])
        return month_period(year, month) if year_ok(year) else None
    if kind == "year_name_month":
        year, month = int(groups[0]), MONTHS[groups[1].lower()]
        return month_period(year, month) if year_ok(year) else None
    if kind == "quarter_first":
        quarter, year = int(groups[0]), int(groups[1])
        return quarter_period(year, quarter) if year_ok(year) else None
    if kind == "quarter_last":
        year, quarter = int(groups[0]), int(groups[1])
        return quarter_period(year, quarter) if year_ok(year) else None
    if kind in {"half_first", "half_last"}:
        half, year = (int(groups[0]), int(groups[1])) if kind == "half_first" else (int(groups[1]), int(groups[0]))
        if not year_ok(year):
            return None
        start_month = 1 if half == 1 else 7
        end_month = start_month + 5
        return Period(f"{year}-H{half}", date(year, start_month, 1),
                      date(year, end_month, calendar.monthrange(year, end_month)[1]), "half")
    if kind == "fiscal_year":
        year = int(groups[0])
        return Period(f"FY{year}", date(year, 1, 1), date(year, 12, 31), "year") if year_ok(year) else None
    if kind == "fiscal_short":
        year = 2000 + int(groups[0])
        return Period(f"FY{year}", date(year, 1, 1), date(year, 12, 31), "year") if year_ok(year) else None
    if kind == "split_year":
        start, suffix = int(groups[0]), int(groups[1])
        end = (start // 100) * 100 + suffix
        if end == start + 1 and year_ok(start):
            return Period(f"{start}-{groups[1]}", date(start, 1, 1), date(end, 12, 31), "range")
        return None
    if kind == "year_range":
        start, end = int(groups[0]), int(groups[1])
        return range_period(start, end) if year_ok(start) and year_ok(end) and start != end else None
    if kind == "year":
        year = int(groups[0])
        return year_period(year) if year_ok(year) else None
    if kind == "day_name_month_year":
        day, month, year = int(groups[0]), MONTHS[groups[1].lower()], int(groups[2])
        value = _safe_date(year, month, day)
        return day_period(value) if value and year_ok(year) else None
    if kind == "name_month_day_year":
        month, day, year = MONTHS[groups[0].lower()], int(groups[1]), int(groups[2])
        value = _safe_date(year, month, day)
        return day_period(value) if value and year_ok(year) else None
    if kind == "numeric_day":
        first, second, year = int(groups[0]), int(groups[1]), int(groups[2])
        # Only unambiguous numeric dates are treated as days. 03/04/2024 could
        # be either convention, so it is reduced to the year it certainly names.
        if first > 12 and second <= 12:
            value = _safe_date(year, second, first)
        elif second > 12 and first <= 12:
            value = _safe_date(year, first, second)
        else:
            return year_period(year) if year_ok(year) else None
        return day_period(value) if value and year_ok(year) else None
    return None


_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.I), kind) for pattern, kind in (
        (r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)", "iso_day"),
        (rf"\b({_MONTH_PATTERN})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+{_YEAR}", "name_month_day_year"),
        (rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_PATTERN})\.?,?\s+{_YEAR}", "day_name_month_year"),
        (rf"\b({_MONTH_PATTERN})\.?,?\s+{_YEAR}", "name_month_year"),
        (rf"\bq([1-4])\s*(?:fy)?\s*{_YEAR}", "quarter_first"),
        (rf"{_YEAR}\s*q([1-4])\b", "quarter_last"),
        (rf"\bh([12])\s+{_YEAR}", "half_first"),
        (r"\bfy\s?(\d{4})\b", "fiscal_year"),
        (rf"{_YEAR}\s*(?:-|–|—|to|through|until)\s*{_YEAR}", "year_range"),
        (_YEAR, "year"),
    )
)


def find_periods(text: object, today: date | None = None) -> list[Period]:
    """Find every distinct period mentioned in free text, most specific first."""
    source = str(text or "")
    if not source:
        return []
    taken: list[tuple[int, int]] = []
    found: list[tuple[int, Period]] = []
    for pattern, kind in _TEXT_PATTERNS:
        for match in pattern.finditer(source):
            span = match.span()
            if any(span[0] < end and span[1] > start for start, end in taken):
                continue
            period = _build(kind, match.groups(), today)
            if period is None:
                continue
            taken.append(span)
            found.append((span[0], period))
    found.sort(key=lambda item: item[0])
    unique: dict[str, Period] = {}
    for _, period in found:
        unique.setdefault(period.label, period)
    return list(unique.values())


def parse_datetime(value: object) -> datetime | None:
    """Parse provenance timestamps (ISO, HTTP-date, or human dates) to UTC."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError, IndexError):
        pass
    cleaned = re.sub(r",?\s+\d{1,2}:\d{2}.*$", "", text)
    period = parse_period(cleaned)
    if period is None:
        periods = find_periods(cleaned)
        period = periods[0] if periods else None
    if period is not None and period.granularity in {"day", "month", "year"}:
        return datetime(period.start.year, period.start.month, period.start.day, tzinfo=UTC)
    return None


def iso_date(value: object) -> str | None:
    parsed = parse_datetime(value)
    return parsed.date().isoformat() if parsed else None


# ----------------------------------------------------------------------------
# Request-level time windows
# ----------------------------------------------------------------------------

_UNIT = r"(years?|yrs?|months?|quarters?|weeks?|days?|decades?)"
_COUNT = r"(\d{1,3}|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")"
_RELATIVE = re.compile(
    rf"\b(?:last|past|previous|prior|recent|preceding)\s+(?:{_COUNT}\s+)?(complete(?:d)?\s+|full\s+|calendar\s+)?{_UNIT}\b",
    re.I,
)
_RANGE = re.compile(
    rf"(?:\bfrom\s+|\bbetween\s+)?{_YEAR}\s*(?:-|–|—|to|through|until|and|\.\.)\s*{_YEAR}", re.I
)
_SINCE = re.compile(rf"\b(?:since|from|after|starting(?:\s+in)?)\s+{_YEAR}", re.I)


def _count(value: str | None) -> int:
    if not value:
        return 1
    value = value.lower()
    return int(value) if value.isdigit() else _NUMBER_WORDS.get(value, 1)


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _year_window(expression: str, start: int, end: int, relative: bool, policy: str) -> TimeWindow:
    return TimeWindow(
        expression=expression, start=date(start, 1, 1), end=date(end, 12, 31),
        granularity="year", relative=relative, policy=policy,
        periods=tuple(str(year) for year in range(start, end + 1)),
    )


def resolve_time_window(text: str, today: date | None = None) -> TimeWindow | None:
    """Resolve the time constraint of a natural-language request deterministically."""
    today = today or today_utc()
    source = " ".join(str(text or "").split())
    if not source:
        return None

    relative = _RELATIVE.search(source)
    if relative:
        count_text, complete_text, unit = relative.groups()
        count = max(1, min(_count(count_text), 500))
        complete = bool(complete_text and complete_text.strip().lower() in {"complete", "completed", "full"})
        unit = unit.lower()
        expression = relative.group(0)
        bare_year = unit.startswith(("year", "yr")) and not count_text and not complete
        if unit.startswith("decade"):
            count, unit = count * 10, "years"
        if unit.startswith(("year", "yr")):
            # "last year" without a number means the previous calendar year.
            if bare_year:
                year = today.year - 1
                return _year_window(expression, year, year, True, "The previous calendar year")
            end = today.year - 1 if complete else today.year
            policy = (f"{count} complete calendar years ending {end}" if complete
                      else f"{count} calendar years ending with the current year ({end}), inclusive")
            return _year_window(expression, end - count + 1, end, True, policy)
        if unit.startswith("month"):
            end_year, end_month = (_shift_month(today.year, today.month, -1)
                                   if complete else (today.year, today.month))
            start_year, start_month = _shift_month(end_year, end_month, -(count - 1))
            periods = []
            for offset in range(count):
                year, month = _shift_month(start_year, start_month, offset)
                periods.append(f"{year}-{month:02d}")
            return TimeWindow(
                expression, date(start_year, start_month, 1),
                month_period(end_year, end_month).end, "month", True,
                f"{count} calendar months ending {end_year}-{end_month:02d}, inclusive", tuple(periods),
            )
        if unit.startswith("quarter"):
            current = (today.month - 1) // 3
            index = today.year * 4 + current - (1 if complete else 0)
            periods = []
            for offset in range(count - 1, -1, -1):
                year, quarter = divmod(index - offset, 4)
                periods.append(quarter_period(year, quarter + 1))
            return TimeWindow(
                expression, periods[0].start, periods[-1].end, "quarter", True,
                f"{count} calendar quarters ending {periods[-1].label}, inclusive",
                tuple(period.label for period in periods),
            )
        days = count * 7 if unit.startswith("week") else count
        start = today - timedelta(days=days - 1)
        listed = tuple((start + timedelta(days=offset)).isoformat() for offset in range(days)) if days <= 400 else ()
        return TimeWindow(expression, start, today, "day", True,
                          f"{days} days ending today ({today.isoformat()}), inclusive", listed)

    explicit_range = _RANGE.search(source)
    if explicit_range:
        first, second = sorted((int(explicit_range.group(1)), int(explicit_range.group(2))))
        if _valid_year(first, today) and _valid_year(second, today) and first != second:
            return _year_window(explicit_range.group(0).strip(), first, second, False,
                                f"Calendar years {first}–{second}, inclusive")

    since = _SINCE.search(source)
    if since:
        start = int(since.group(1))
        if _valid_year(start, today) and start <= today.year:
            return _year_window(since.group(0), start, today.year, True,
                                f"{start} through the current year ({today.year}), inclusive")

    lowered = source.lower()
    if re.search(r"\b(?:year[\s-]to[\s-]date|ytd)\b", lowered):
        return TimeWindow("year to date", date(today.year, 1, 1), today, "day", True,
                          f"1 January {today.year} through today", ())
    if re.search(r"\b(?:this|current)\s+year\b", lowered):
        return _year_window("this year", today.year, today.year, True, "The current calendar year")
    if re.search(r"\b(?:this|current)\s+month\b", lowered):
        period = month_period(today.year, today.month)
        return TimeWindow("this month", period.start, period.end, "month", True,
                          "The current calendar month", (period.label,))
    if re.search(r"\b(?:last|previous)\s+month\b", lowered):
        year, month = _shift_month(today.year, today.month, -1)
        period = month_period(year, month)
        return TimeWindow("last month", period.start, period.end, "month", True,
                          "The previous calendar month", (period.label,))

    periods = [period for period in find_periods(source, today) if period.granularity != "day"]
    if periods:
        start = min(period.start for period in periods)
        end = max(period.end for period in periods)
        if all(period.granularity == "year" for period in periods):
            years = sorted({period.start.year for period in periods})
            window = _year_window(", ".join(str(year) for year in years), years[0], years[-1], False,
                                  "Explicitly requested calendar years")
            return window
        granularity = periods[0].granularity if len(periods) == 1 else "range"
        return TimeWindow(", ".join(period.label for period in periods), start, end, granularity,
                          False, "Explicitly requested periods",
                          tuple(period.label for period in periods))
    return None


def strip_time_expressions(text: str) -> str:
    """Remove temporal phrases so the remaining words describe subject and measure."""
    value = _RELATIVE.sub(" ", text)
    value = _RANGE.sub(" ", value)
    value = _SINCE.sub(" ", value)
    value = re.sub(r"\b(?:this|current|last|previous|past)\s+(?:year|month|decade)\b", " ", value, flags=re.I)
    value = re.sub(r"\b(?:year[\s-]to[\s-]date|ytd)\b", " ", value, flags=re.I)
    value = re.sub(_YEAR, " ", value)
    return " ".join(value.split())


# ----------------------------------------------------------------------------
# Observation-level temporal resolution
# ----------------------------------------------------------------------------

_HEADER_YEAR = re.compile(r"(?<!\d)(?:18|19|20)\d{2}(?!\d)")


def _header_year(name: str | None, today: date | None) -> int | None:
    """The one plausible year named in a column header, if exactly one."""
    if not name:
        return None
    latest = (today or today_utc()).year + 1
    years = {int(match) for match in _HEADER_YEAR.findall(name) if 1800 <= int(match) <= latest}
    return years.pop() if len(years) == 1 else None


def _single(periods: Iterable[Period]) -> Period | None:
    unique = {period.label: period for period in periods}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _combine_year_and_month(fields: Mapping[str, str], time_keys: Iterable[str]) -> Period | None:
    year = month = None
    for key in time_keys:
        value = str(fields.get(key, "")).strip()
        if "year" in key and re.fullmatch(r"(?:19|20)\d{2}", value):
            year = int(value)
        if "month" in key:
            lowered = value.lower().rstrip(".")
            if lowered in MONTHS:
                month = MONTHS[lowered]
            elif value.isdigit() and 1 <= int(value) <= 12:
                month = int(value)
    return month_period(year, month) if year and month else None


def resolve_observation_period(
    fields: Mapping[str, str],
    time_keys: Iterable[str],
    *,
    context: str = "",
    context_kind: str = "",
    page_title: str = "",
    temporal_coverage: str | None = None,
    published_at: str | None = None,
    modified_at: str | None = None,
    fetched_at: datetime | str | None = None,
    has_measure: bool = True,
    today: date | None = None,
    measure_field: str | None = None,
) -> TemporalResolution:
    """Decide which period an observation describes, and how sure we are.

    Precedence: explicit row field -> explicit period in another row value ->
    a single year in the measured column's header ("2011 census population")
    -> table caption/header -> nearby heading -> page title -> structured page
    metadata -> snapshot inference from modified/published/fetched dates.
    Page dates are provenance; they only become the observation period when the
    row carries no period of its own and plausibly describes a current snapshot.
    """
    time_keys = [key for key in time_keys if key in fields]

    combined = _combine_year_and_month(fields, time_keys)
    if combined:
        return TemporalResolution(combined, "row_field", False, 0.95, "Year and month columns")
    for key in time_keys:
        period = parse_period(fields[key], today)
        if period:
            confidence = 0.95 if period.granularity != "range" else 0.85
            return TemporalResolution(period, "row_field", False, confidence, f"Column '{key}'")
    for key in time_keys:
        period = _single(find_periods(fields[key], today))
        if period:
            return TemporalResolution(period, "row_field", False, 0.85, f"Column '{key}'")

    value_periods: list[Period] = []
    for key, value in fields.items():
        if key in time_keys or key in {"url", "link", "href", "image"}:
            continue
        value_periods.extend(find_periods(value, today))
    row_period = _single(value_periods)
    if row_period:
        return TemporalResolution(row_period, "row_value", False, 0.8, "Period stated in the row")
    row_has_many_periods = len({period.label for period in value_periods}) > 1

    header_year = _header_year(measure_field, today)
    if header_year is not None and not row_has_many_periods:
        return TemporalResolution(year_period(header_year), "column_header", False, 0.85,
                                  f"Year in column '{measure_field}'")

    if context and not row_has_many_periods:
        period = _single(find_periods(context, today))
        if period and period.granularity != "range":
            if context_kind == "caption":
                return TemporalResolution(period, "table_header", False, 0.8, "Table caption")
            return TemporalResolution(period, "context", True, 0.6, "Nearest heading")

    if not has_measure or row_has_many_periods:
        return TemporalResolution(None, "none", False, 0.0, "No period stated")

    title_periods = find_periods(page_title, today)
    if title_periods:
        period = _single(title_periods)
        if period is None or period.granularity == "range":
            # A multi-period page (e.g. "2010–2025 trend") is a time series; a
            # row without its own period cannot be pinned to one of them.
            return TemporalResolution(None, "none", False, 0.0, "Page spans several periods")
        return TemporalResolution(period, "page_context", True, 0.5, "Page title")

    if temporal_coverage:
        period = parse_period(temporal_coverage, today) or _single(find_periods(temporal_coverage, today))
        if period:
            return TemporalResolution(period, "page_metadata", True, 0.55, "Structured temporal coverage")

    today = today or today_utc()
    for basis, stamp, confidence in (
        ("modified_at", modified_at, 0.45),
        ("published_at", published_at, 0.4),
        ("fetched_at", fetched_at, 0.25),
    ):
        parsed = stamp if isinstance(stamp, datetime) else parse_datetime(stamp)
        if parsed is None:
            continue
        age_years = max(0, today.year - parsed.year)
        adjusted = round(max(0.1, confidence - 0.1 * max(0, age_years - 1)), 2)
        return TemporalResolution(year_period(parsed.year), basis, True, adjusted,
                                  "Current snapshot inferred from page dates")
    return TemporalResolution(None, "none", False, 0.0, "No period stated")
