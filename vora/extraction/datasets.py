"""Machine-readable datasets linked from rendered pages.

Many statistics pages draw their numbers as interactive charts, so the HTML
holds only chart controls. Publishers usually offer the same data as a
download. This module finds those downloads, ranks them by relevance to the
plan, and turns CSV files into observations. It performs no network I/O: the
coordinator fetches what this module selects.

Two kinds of link are recognised:

* explicit data files anywhere on the page (``.csv`` / ``.tsv`` links, or
  links with ``format=csv``);
* embedded chart platforms with a documented CSV counterpart. Our World in
  Data charts (``/grapher/<slug>``) and explorers (``/explorers/<slug>``) are
  published as ``<same path>.csv`` under an open licence.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from vora.extraction.semantics import content_tokens, has_quantity, tokens, value_kind
from vora.extraction.temporal import parse_period

MAX_ROWS_PER_DATASET = 1500
MAX_MEASURE_COLUMNS = 16
_DATA_EXTENSIONS = (".csv", ".tsv")


@dataclass(frozen=True, slots=True)
class DatasetLink:
    url: str                 # the downloadable file
    title: str               # human label: link text, chart slug, embed title
    page_url: str            # where the link was found
    kind: str                # file | chart
    metadata_url: str | None = None
    primary: bool = False    # the page's main embedded chart


@dataclass(slots=True)
class DatasetTable:
    rows: list[dict[str, str]] = field(default_factory=list)
    kept: int = 0
    dropped_out_of_window: int = 0
    total: int = 0


def _slug_title(path: str) -> str:
    slug = path.rstrip("/").rsplit("/", 1)[-1]
    return slug.replace("-", " ").strip().capitalize()


def _owid_csv(url: str) -> tuple[str, str | None] | None:
    """Documented CSV endpoint for an Our World in Data chart or explorer."""
    parsed = urlparse(url)
    if not parsed.netloc.endswith("ourworldindata.org"):
        return None
    match = re.match(r"^/(grapher|explorers)/([a-z0-9-]+)/?$", parsed.path)
    if not match:
        return None
    kind, slug = match.groups()
    # Keep the chart's selections (e.g. Crop=Wheat) but request every
    # country and the full series rather than the on-screen subset.
    query = [(key, value) for key, value in parse_qsl(parsed.query)
             if key.lower() not in {"country", "hidecontrols", "tab", "facet", "time"}]
    query += [("csvType", "full"), ("useColumnShortNames", "false")]
    csv_url = urlunparse(parsed._replace(path=f"/{kind}/{slug}.csv", query=urlencode(query), fragment=""))
    metadata = urlunparse(parsed._replace(path=f"/{kind}/{slug}.metadata.json", query="", fragment="")) \
        if kind == "grapher" else None
    return csv_url, metadata


def find_dataset_links(soup: BeautifulSoup, page_url: str, limit: int = 60) -> list[DatasetLink]:
    """Downloadable datasets linked or embedded in a rendered page."""
    links: dict[str, DatasetLink] = {}

    def add(link: DatasetLink) -> None:
        if link.url not in links and len(links) < limit:
            links[link.url] = link

    # Embedded charts come first: they are what the page actually shows.
    for node in soup.select("[data-explorer-src], [data-grapher-src]"):
        src = node.get("data-explorer-src") or node.get("data-grapher-src") or ""
        resolved = _owid_csv(urljoin(page_url, src))
        if resolved:
            title = node.get_text(" ", strip=True)[:80] or _slug_title(urlparse(src).path)
            add(DatasetLink(resolved[0], _slug_title(urlparse(src).path) if len(title) > 60 else title,
                            page_url, "chart", resolved[1], primary=True))

    for anchor in soup.select("a[href], iframe[src]"):
        href = urljoin(page_url, anchor.get("href") or anchor.get("src") or "")
        parsed = urlparse(href)
        if parsed.scheme not in {"http", "https"}:
            continue
        text = " ".join(anchor.get_text(" ", strip=True).split())[:120]
        resolved = _owid_csv(href)
        if resolved:
            add(DatasetLink(resolved[0], text or _slug_title(parsed.path), page_url, "chart", resolved[1]))
            continue
        query = dict(parse_qsl(parsed.query))
        if parsed.path.lower().endswith(_DATA_EXTENSIONS) or \
                query.get("format", "").lower() in {"csv", "tsv"}:
            add(DatasetLink(href.split("#", 1)[0], text or _slug_title(parsed.path), page_url, "file"))
    return list(links.values())


def rank_links(links: list[DatasetLink], terms: list[str], limit: int) -> list[DatasetLink]:
    """Most relevant links first: plan terms in the title or URL, primary embed as tie-break."""
    wanted = {token for term in terms for token in content_tokens(term)}

    def score(link: DatasetLink) -> tuple[int, int, int]:
        words = set(tokens(f"{link.title} {urlparse(link.url).path}"))
        overlap = len(wanted & words)
        return overlap, int(link.primary), -len(link.title)

    ranked = sorted(links, key=score, reverse=True)
    return [link for link in ranked if score(link)[0] > 0 or link.primary][:limit]


def _period_column(header: list[str], sample: list[list[str]]) -> int | None:
    for index, name in enumerate(header):
        if tokens(name) and tokens(name)[0] in {"year", "date", "period", "time", "day", "month", "quarter"}:
            return index
    for index in range(len(header)):
        values = [row[index] for row in sample if index < len(row) and row[index]]
        if values and all(parse_period(value) for value in values):
            return index
    return None


def parse_csv(text: str, *, window: tuple[str, str] | None = None,
              places: list[str] | None = None) -> DatasetTable:
    """CSV/TSV text -> observation field dicts (see ``parse_matrix``)."""
    delimiter = "	" if text[:2000].count("	") > text[:2000].count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = [cell.strip() for cell in next(reader)]
    except StopIteration:
        return DatasetTable()
    body = [row for row in reader if any(cell.strip() for cell in row)]
    return parse_matrix(header, body, window=window, places=places)


def _unpivot_period_columns(header: list[str], body: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """Spreadsheets often put years across the top ("Country | 2019 | 2020 …").
    Turn such files into one row per period so each value carries its period."""
    period_columns = [index for index, name in enumerate(header) if parse_period(name)]
    if len(period_columns) < 3 or len(period_columns) < 0.5 * (len(header) - 1):
        return header, body
    labels = [index for index in range(len(header)) if index not in period_columns]
    long_header = [header[index] or "series" for index in labels] + ["period", "value"]
    long_body = []
    for row in body:
        base = [row[index] if index < len(row) else "" for index in labels]
        for index in period_columns:
            if index < len(row) and str(row[index]).strip():
                long_body.append([*base, header[index], str(row[index]).strip()])
    return long_header, long_body


def parse_matrix(header: list[str], body: list[list[str]], *, window: tuple[str, str] | None = None,
                 places: list[str] | None = None) -> DatasetTable:
    """A table (header + rows) -> observation field dicts, one per (row, measure).

    Rows are limited to the requested time window (ISO start/end dates) and,
    when the plan names places or entities that occur in the file, to those.
    Wide files with several measures ("Wheat", "Rice", …) become one
    observation per measure; files with periods as columns are unpivoted.
    """
    header = [str(cell or "").strip() for cell in header]
    body = [[str(cell if cell is not None else "").strip() for cell in row] for row in body]
    body = [row for row in body if any(row)]
    header, body = _unpivot_period_columns(header, body)
    table = DatasetTable(total=len(body))
    if not header or not body:
        return table
    period_index = _period_column(header, body[:50])
    # A measure column holds only quantities wherever it has a value; sparse
    # columns (a crop not grown everywhere) are still measures.
    sample = body[:3000]

    def is_measure(index: int) -> bool:
        values = [row[index] for row in sample if index < len(row) and row[index]]
        return bool(values) and sum(1 for value in values if has_quantity(value)) >= len(values) * 0.95

    measures = [index for index in range(len(header)) if index != period_index and is_measure(index)]
    measures = measures[:MAX_MEASURE_COLUMNS]
    labels = [index for index in range(len(header)) if index not in measures and index != period_index]
    if not measures:
        return table

    wanted_places = {place.casefold() for place in (places or [])}
    if wanted_places:
        present = {row[index].casefold() for row in body for index in labels if index < len(row)}
        wanted_places &= present  # only filter on places that actually occur

    start, end = window if window else ("", "")
    selected = []
    for row in body:
        if period_index is not None and window:
            period = parse_period(row[period_index]) if period_index < len(row) else None
            if period is None or period.end.isoformat() < start or period.start.isoformat() > end:
                table.dropped_out_of_window += 1
                continue
        if wanted_places and not any(index < len(row) and row[index].casefold() in wanted_places
                                     for index in labels):
            continue
        selected.append(row)
    # Latest periods first so the cap keeps the most recent data.
    if period_index is not None:
        selected.sort(key=lambda row: row[period_index] if period_index < len(row) else "", reverse=True)

    for row in selected:
        base = {(header[index] or f"column_{index + 1}"): row[index] for index in labels
                if index < len(row) and row[index] and value_kind(row[index]) != "empty"}
        if period_index is not None and period_index < len(row):
            base[header[period_index]] = row[period_index]
        for index in measures:
            if index < len(row) and row[index]:
                if len(measures) == 1:
                    table.rows.append({**base, header[index] or "value": row[index]})
                else:
                    # Several measures ("Wheat", "Rice"): the column names are
                    # series, and the dataset title says what the values measure.
                    table.rows.append({**base, "series": header[index], "value": row[index]})
        if len(table.rows) >= MAX_ROWS_PER_DATASET:
            break
    table.kept = len(table.rows)
    return table
