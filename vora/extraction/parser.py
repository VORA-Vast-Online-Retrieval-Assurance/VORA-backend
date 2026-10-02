"""Domain-neutral parsing of rendered engine output.

Parsing turns a rendered page into raw observations and never decides whether
they answer the request; that is the scorer's job. It does, however, record
everything the scorer needs: page provenance (titles and dates), the caption
or heading a table sits under, the extraction method's reliability, and a
structural noise verdict so obvious chrome never reaches semantic scoring.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from vora.browser.contracts import ExecutionResult
from vora.shared.contracts import GoalPlan, Observation

from vora.extraction.datasets import DatasetLink, find_dataset_links
from vora.extraction.files import FoundFile, file_extension, find_file_links
from vora.extraction.sites import adapter_for
from vora.extraction.noise import NOISE_ROLES, NOISE_TAGS, assess, is_challenge_page, is_login_wall, is_noise_class
from vora.extraction.semantics import UNIT_TOKENS, value_kind
from vora.extraction.temporal import find_periods, iso_date, parse_period

MAX_OBSERVATIONS = 500

# Prior reliability of each extraction method before looking at the content.
METHOD_CONFIDENCE = {
    "linked_dataset": 0.95,  # the publisher's own machine-readable download
    "spreadsheet": 0.9,
    "structured_data": 0.85,
    "site_adapter": 0.85,
    "pdf_table": 0.8,
    "pdf_text": 0.5,
    "html_table": 0.9,
    "json_ld": 0.85,
    "repeated_region": 0.6,
    "text_statement": 0.55,
    "list_item": 0.65,          # an entry of a repeated list of links (documents, pages)
    "undated_statement": 0.45,  # the period is inferred from the page dates
    "network_json": 0.85,
    "hydration_json": 0.85,     # data a JavaScript app ships inside the page (__NEXT_DATA__ and similar)       # data a page's charts load (captured by the interactive pass)
    "page_summary": 0.25,
}
# Methods whose rows are sentences (attribution applies to the statement text).
STATEMENT_METHODS = frozenset({"text_statement", "undated_statement", "pdf_text"})

PROPERTY_HEADERS = {"field", "property", "attribute", "key", "name", "label", "item", "parameter",
                    "specification", "spec", "metric", "indicator", "characteristic"}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:1000]


def _key(value: str) -> str:
    return re.sub(r"\W+", "_", str(value).casefold()).strip("_")


def _flatten(value: Any, prefix: str = "", depth: int = 0) -> dict[str, str]:
    if depth > 3:
        return {}
    if isinstance(value, dict):
        out: dict[str, str] = {}
        for key, item in list(value.items())[:30]:
            key = str(key).lstrip("@")
            name = f"{prefix}_{key}" if prefix else key
            if isinstance(item, (dict, list)):
                out.update(_flatten(item, name, depth + 1))
            elif _clean(item):
                out[name] = _clean(item)
        return out
    if isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value[:5]) and depth < 3:
            out = {}
            for item in value[:5]:
                for key, item_value in _flatten(item, prefix, depth + 1).items():
                    out.setdefault(key, item_value)
            return out
        return {prefix or "values": _clean(", ".join(map(str, value[:20])))}
    return {prefix or "value": _clean(value)} if _clean(value) else {}


# ----------------------------------------------------------------------------
# Page provenance
# ----------------------------------------------------------------------------

def _meta(soup: BeautifulSoup, *names: str) -> str | None:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name}) \
            or soup.find("meta", attrs={"itemprop": name})
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    return None


def _jsonld_objects(soup: BeautifulSoup) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for script in soup.select('script[type="application/ld+json"]')[:30]:
        try:
            payload = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        stack = payload if isinstance(payload, list) else [payload]
        for item in stack:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                objects.extend(entry for entry in graph if isinstance(entry, dict))
            else:
                objects.append(item)
    return objects


def page_provenance(soup: BeautifulSoup, result: ExecutionResult) -> dict[str, Any]:
    """Collect page-level provenance from meta tags, JSON-LD and HTTP headers."""
    objects = _jsonld_objects(soup)

    def from_jsonld(*keys: str) -> str | None:
        for item in objects:
            for key in keys:
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    headers = result.metadata.get("response_headers", {}) if result.metadata else {}
    header_modified = next((value for key, value in dict(headers).items()
                            if key.casefold() == "last-modified"), None)
    time_tag = soup.find("time", attrs={"datetime": True})
    published = (
        _meta(soup, "article:published_time", "datePublished", "date", "pubdate", "publishdate",
              "dc.date", "DC.date.issued", "dcterms.created", "citation_publication_date",
              "og:published_time")
        or from_jsonld("datePublished", "schema:datePublished", "dateCreated")
        or (time_tag.get("datetime") if time_tag else None)
    )
    modified = (
        _meta(soup, "article:modified_time", "og:updated_time", "dateModified", "last-modified",
              "dcterms.modified", "DC.date.modified")
        or from_jsonld("dateModified", "schema:dateModified")
        or header_modified
    )
    site_name = _meta(soup, "og:site_name", "application-name")
    if not site_name:
        for item in objects:
            publisher = item.get("publisher")
            if isinstance(publisher, dict) and isinstance(publisher.get("name"), str):
                site_name = publisher["name"]
                break
    title = _clean(_meta(soup, "og:title") or (soup.title.get_text() if soup.title else "") or result.title)
    fetched = result.metadata.get("fetched_at") if result.metadata else None
    heading = soup.find("h1")
    return {
        "title": title[:300],
        "heading": _clean(heading.get_text(" ", strip=True))[:300] if heading else "",
        "site_name": _clean(site_name)[:120] if site_name else "",
        "published_at": iso_date(published),
        "modified_at": iso_date(modified),
        "fetched_at": fetched or datetime.now(UTC).isoformat(),
        "temporal_coverage": from_jsonld("temporalCoverage", "schema:temporalCoverage"),
        "domain": urlparse(result.final_url).netloc.removeprefix("www."),
    }


# ----------------------------------------------------------------------------
# Noise regions
# ----------------------------------------------------------------------------

def _is_noise_node(node: Tag) -> bool:
    if not isinstance(node, Tag) or node.name in {"html", "body", "main"}:
        return False
    if node.name in NOISE_TAGS:
        return True
    if str(node.get("role", "")).casefold() in NOISE_ROLES:
        return True
    if str(node.get("aria-hidden", "")).casefold() == "true":
        return True
    if node.name == "header" and not node.find_parent(["article", "main"]):
        return True
    return is_noise_class(node.get("class")) or is_noise_class(node.get("id"))


def _text_length(node: Tag) -> int:
    return len(node.get_text(" ", strip=True))


def _inside_noise(node: Tag, page_length: int) -> bool:
    """True when an ancestor is chrome. A region holding over half of the page's
    text is never treated as chrome, whatever its class names say."""
    return any(_is_noise_node(parent) and _text_length(parent) <= page_length * 0.5
               for parent in node.parents if isinstance(parent, Tag))


def _remove_noise(soup: BeautifulSoup) -> None:
    """Drop chrome in place, but never a region holding most of the page's text."""
    for tag in soup(["script", "style", "svg", "noscript", "template", "iframe"]):
        tag.decompose()
    body = soup.body or soup
    total = max(1, _text_length(body))
    for node in [node for node in body.find_all(True) if _is_noise_node(node)]:
        if getattr(node, "decomposed", False) or node.parent is None:
            continue
        if _text_length(node) <= total * 0.5:
            node.decompose()


def _context_for(node: Tag) -> tuple[str, str]:
    caption = node.find("caption") if node.name == "table" else None
    if caption and _clean(caption.get_text(" ", strip=True)):
        return _clean(caption.get_text(" ", strip=True))[:240], "caption"
    heading = node.find_previous(["h1", "h2", "h3", "h4"])
    if heading:
        return _clean(heading.get_text(" ", strip=True))[:240], "heading"
    return "", ""


# ----------------------------------------------------------------------------
# Tables
# ----------------------------------------------------------------------------

def _span(cell: Tag) -> int:
    try:
        return max(1, min(int(cell.get("colspan", 1) or 1), 20))
    except (TypeError, ValueError):
        return 1


class _Cell(str):
    """Cell text that remembers the first link inside the cell."""

    href: str = ""

    def __new__(cls, text: str, href: str = "") -> "_Cell":
        instance = super().__new__(cls, text)
        instance.href = href
        return instance


_SKIPPED_HREF = ("javascript:", "#", "data:", "tel:", "vbscript:")


def _link_of(node: Tag) -> tuple[str, str]:
    """(text, href) of the first real link inside a node, or ("", "")."""
    for anchor in node.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        if href and not href.casefold().startswith(_SKIPPED_HREF):
            return _clean(anchor.get_text(" ", strip=True)), href
    return "", ""


def _link_name(text: str, href: str, base: str) -> str:
    """Field name for a link: a file is a "document", another site's generic
    link ("Official website") is that record's "website", anything else "url"."""
    from vora.extraction.records import is_generic_link_label

    if file_extension(href):
        return "document"
    same_site = (urlparse(href).hostname or "").removeprefix("www.") == (urlparse(base).hostname or "").removeprefix("www.")
    if not same_site and urlparse(href).scheme in {"http", "https"} and is_generic_link_label(text):
        return "website"
    return "url"


def _matrix(table: Tag) -> list[tuple[list[str], bool]]:
    rows: list[tuple[list[str], bool]] = []
    for row in table.find_all("tr"):
        if row.find_parent("table") is not table:
            continue
        cells = row.find_all(["th", "td"], recursive=False)
        if not cells:
            continue
        in_head = row.find_parent("thead") is not None
        all_th = all(cell.name == "th" for cell in cells)
        values: list[str] = []
        for cell in cells:
            text = _clean(cell.get_text(" ", strip=True))
            span = _span(cell)
            marked = _Cell(text, _link_of(cell)[1]) if not (in_head or all_th) else text
            values.extend([marked] * span if (in_head or all_th) else [marked] + [""] * (span - 1))
        if any(values):
            rows.append((values, in_head or all_th))
    return rows


def _unique_headers(headers: list[str]) -> list[str]:
    seen: Counter[str] = Counter()
    result = []
    for index, header in enumerate(headers):
        name = header or ("label" if index == 0 else f"column_{index + 1}")
        seen[name] += 1
        result.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    return result


def _resolve_headers(matrix: list[tuple[list[str], bool]]) -> tuple[list[str] | None, list[list[str]], str]:
    """Return (headers, body rows, title). A leading row whose single value
    spans every column ("Top 20 used cars, June 2026") is a title, not a header."""
    width = max(len(values) for values, _ in matrix)
    padded = [(values + [""] * (width - len(values)), header) for values, header in matrix]
    title = ""
    while padded and width > 1 and len(padded) > 2:
        values = [cell for cell in padded[0][0] if cell]
        if len(set(values)) == 1 and (len(values) == width or len(values) == 1) and (
                value_kind(values[0]) == "text"):
            title = f"{title} {values[0]}".strip()
            padded = padded[1:]
            continue
        break
    header_rows = []
    for values, is_header in padded[:3]:
        if not is_header:
            break
        header_rows.append(values)
    body = [values for values, _ in padded[len(header_rows):]]
    if not header_rows and len(padded) >= 2:
        first = padded[0][0]
        texts = [cell for cell in first if cell]
        body_has_values = any(value_kind(cell) in {"money", "percent", "number", "period"}
                              for values, _ in padded[1:4] for cell in values)
        first_is_text = texts and all(value_kind(cell) in {"text", "period"} for cell in texts)
        periods_in_first = sum(1 for cell in texts if parse_period(cell))
        if first_is_text and body_has_values and len(set(texts)) == len(texts) and \
                (periods_in_first == 0 or periods_in_first >= 3):
            header_rows = [first]
            body = [values for values, _ in padded[1:]]
    if not header_rows:
        return None, body, title
    headers = []
    for column in range(width):
        parts = list(dict.fromkeys(row[column] for row in header_rows if row[column]))
        headers.append(" ".join(parts))
    return _unique_headers(headers), body, title


def _table_observations(table: Tag, base: str = "") -> tuple[list[dict[str, str]], str]:
    """Rows of a table plus any title found in its header rows.

    A cell that links somewhere keeps the link: a label such as "Download" is
    replaced by the address, any other text keeps its text and gains a
    ``<column>_url`` field.
    """
    from vora.extraction.records import is_generic_link_label

    matrix = _matrix(table)
    if len(matrix) < 2 and not (matrix and len(matrix[0][0]) >= 2):
        return [], ""
    headers, body, title = _resolve_headers(matrix)
    width = max(len(values) for values, _ in matrix)

    # Two-column property sheets ("Price | $50,000", "Updated | 2026-07-14")
    # describe one thing, so they become a single observation.
    if width == 2:
        header_names = {_key(item) for item in headers} if headers else set()
        labels = [row[0] for row in body if row and row[0]]
        is_property_sheet = (
            (headers is None or header_names & PROPERTY_HEADERS or header_names <= {"label", "column_2"})
            and len(labels) >= 2 and len(set(labels)) == len(labels)
            and all(value_kind(label) == "text" for label in labels)
        )
        if is_property_sheet:
            record = {label: row[1] for row, label in zip(body, labels) if len(row) > 1 and row[1]}
            return ([record] if record else []), title

    if headers is None:
        headers = [f"column_{index + 1}" for index in range(width)]

    rows = []
    for values in body[:MAX_OBSERVATIONS]:
        if values == headers:
            continue
        row: dict[str, str] = {}
        for index, value in enumerate(values[:len(headers)]):
            if not value:
                continue
            header, href = headers[index], getattr(value, "href", "")
            if href:
                address = urljoin(base, href)
                if is_generic_link_label(str(value)):
                    row[header] = address
                else:
                    row[header] = str(value)
                    row[f"{header}_url"] = address
            else:
                row[header] = str(value)
        if row:
            rows.append(row)

    # Wide tables whose columns are periods ("2019 | 2020 | 2021" or
    # "1/2020 | 2/2020 ...") are unpivoted into one observation per cell so each
    # value carries its own period.
    period_columns = [header for header in headers if parse_period(header)]
    if len(period_columns) >= 3 and len(period_columns) >= 0.5 * len(headers):
        label_columns = [header for header in headers if header not in period_columns]
        long_rows = []
        for row in rows:
            labels = {("series" if name == "label" else name): row[name]
                      for name in label_columns if row.get(name)}
            for column in period_columns:
                if row.get(column):
                    long_rows.append({**labels, "period": column, "value": row[column]})
                if len(long_rows) >= MAX_OBSERVATIONS:
                    break
        return long_rows, title
    return rows, title


def _table_rows(soup: BeautifulSoup, base: str = "") -> list[tuple[dict[str, str], str, str, str]]:
    """Rows of every data table as (fields, context, context kind, block id)."""
    gathered = []
    page_length = max(1, _text_length(soup.body or soup))
    for index, table in enumerate(soup.find_all("table")[:40]):
        if table.find_parent("table") is not None or _inside_noise(table, page_length):
            continue
        rows, title = _table_observations(table, base)
        context, kind = (title[:240], "caption") if title else _context_for(table)
        for row in rows:
            gathered.append((row, context, kind, f"table#{index + 1}"))
    return gathered


# ----------------------------------------------------------------------------
# JSON-LD, repeated regions and prose
# ----------------------------------------------------------------------------

def _json_rows(soup: BeautifulSoup) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for script in soup.select('script[type="application/ld+json"]')[:30]:
        try:
            payload = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = payload.get("itemListElement", payload.get("@graph", [payload]))
        else:
            continue
        if not isinstance(items, list):
            items = [items]
        for item in items[:MAX_OBSERVATIONS]:
            if isinstance(item, dict) and isinstance(item.get("item"), dict):
                item = item["item"]
            flattened = _flatten(item)
            if len(flattened) >= 2:
                rows.append(flattened)
    return rows


# JavaScript apps ship their data inside the page before drawing it: Next.js in __NEXT_DATA__, others as
# window.__INITIAL_STATE__ / __NUXT__ / __APOLLO_STATE__, or in application/json script tags.
_STATE_ASSIGNMENT = re.compile(
    r"window\.(__INITIAL_STATE__|__PRELOADED_STATE__|__NUXT__|__APOLLO_STATE__|__DATA__)\s*=\s*(\{.*?\})\s*;?\s*(?:</script>|$)",
    re.S)
MAX_HYDRATION_BYTES = 3_000_000


def _record_lists(payload: object, found: list[list[dict]], depth: int = 0) -> None:
    """Every list of objects inside ``payload`` (the candidates for records)."""
    if isinstance(payload, list) and len(payload) >= 3 and all(isinstance(item, dict) for item in payload[:20]):
        found.append(payload)
    if depth >= 8:
        return
    children = payload.values() if isinstance(payload, dict) else payload if isinstance(payload, list) else []
    for child in list(children)[:200]:
        _record_lists(child, found, depth + 1)


def _hydration_rows(soup: BeautifulSoup) -> list[tuple[dict[str, str], str]]:
    """Records from data a page's JavaScript app embeds, with a label of where they were found."""
    payloads: list[tuple[str, object]] = []
    for script in soup.select('script#__NEXT_DATA__, script[type="application/json"]')[:10]:
        text = script.string or script.get_text()
        if text and len(text) <= MAX_HYDRATION_BYTES:
            try:
                payloads.append((script.get("id") or "application/json", json.loads(text)))
            except ValueError:
                continue
    for script in soup.find_all("script")[:80]:
        text = script.string or ""
        if "window.__" not in text or len(text) > MAX_HYDRATION_BYTES:
            continue
        for match in _STATE_ASSIGNMENT.finditer(text):
            try:
                payloads.append((match.group(1), json.loads(match.group(2))))
            except ValueError:
                continue
    rows: list[tuple[dict[str, str], str]] = []
    for label, payload in payloads:
        lists: list[list[dict]] = []
        _record_lists(payload, lists)
        # The biggest lists are the page's content; small ones are menus and settings.
        for items in sorted(lists, key=len, reverse=True)[:2]:
            for item in items[:MAX_OBSERVATIONS]:
                flattened = _flatten(item)
                if len(flattened) >= 2:
                    rows.append((flattened, label))
            if len(rows) >= MAX_OBSERVATIONS:
                return rows[:MAX_OBSERVATIONS]
    return rows


_GENERIC_CLASSES = frozenset({"text", "label", "value", "item", "col", "cell", "field", "content", "info",
                              "meta", "details", "detail", "wrapper", "inner", "row", "small", "muted"})
_LEAF_BLOCKS = ["p", "span", "div", "li", "time", "small", "td", "strong", "em", "b", "dd", "dt", "label"]


def _leaf_label(node: Tag, used: set[str]) -> str:
    """A field name from an element's class (or tag), unique within its card."""
    names = [re.sub(r"[^a-z0-9]+", "_", str(item).casefold()).strip("_") for item in node.get("class", [])]
    name = next((item for item in names if item and item not in _GENERIC_CLASSES and not item.isdigit()),
                node.name if node.name != "div" else "text")
    unique, count = name, 1
    while unique in used:
        count += 1
        unique = f"{name}_{count}"
    used.add(unique)
    return unique


def _card_rows(soup: BeautifulSoup, base: str = "") -> list[tuple[dict[str, str], str, str, str]]:
    """Repeated cards as (fields, context, context kind, block id: one per card style)."""
    candidates = soup.select("article, [class*='card'], [class*='item'], [class*='product'], [class*='result']")
    signatures = Counter(" ".join(node.get("class", [])) or node.name for node in candidates)
    common = {signature for signature, count in signatures.items() if count >= 2}
    rows = []
    for node in candidates[:300]:
        signature = " ".join(node.get("class", [])) or node.name
        if signature not in common or node.find_parent("table") is not None:
            continue
        fields: dict[str, str] = {}
        heading = node.select_one("h1,h2,h3,h4,[class*='title'],[class*='name']")
        heading_text = _clean(heading.get_text(" ", strip=True)) if heading else ""
        if heading_text:
            fields["name"] = heading_text
        for label in node.select("dt, [class*='label']")[:12]:
            sibling = label.find_next_sibling()
            if sibling:
                fields[_clean(label.get_text(" ", strip=True)).lower().replace(" ", "_")] = \
                    _clean(sibling.get_text(" ", strip=True))
        link_text, href = _link_of(node)
        # Every labelled leaf of the card is a field: its date, city, category...
        used = set(fields) | {"name", "url", "website", "document"}
        labelled = {value for value in fields.values()}
        for leaf in node.find_all(_LEAF_BLOCKS)[:40]:
            if leaf.find(_LEAF_BLOCKS) is not None or leaf is heading:
                continue
            value = _clean(leaf.get_text(" ", strip=True))
            if not value or value in labelled or value == heading_text or value == link_text or len(value) > 300:
                continue
            fields[_leaf_label(leaf, used)] = value
            labelled.add(value)
        if href:
            address = urljoin(base, href)
            fields.setdefault(_link_name(link_text, address, base), address)
        text = _clean(node.get_text(" ", strip=True))
        if text and len(fields) < 2:
            fields["text"] = text
        if len(fields) >= 2:
            context, kind = _context_for(node)
            rows.append((fields, context, kind, f"cards:{signature[:48]}"))
    return rows


_LIST_SEPARATORS = " \u2013\u2014-|:;,.()[]\u00b7\u2022"


def _list_rows(soup: BeautifulSoup, base: str = "") -> list[tuple[dict[str, str], str, str, str]]:
    """Entries of repeated link lists (documents, notices, articles) as records.

    A list qualifies when at least three items each carry a link and read like
    titles, which separates a list of documents from a row of menu buttons
    (menus are removed as page chrome before this runs).
    """
    from vora.extraction.records import is_generic_link_label

    rows = []
    for index, listing in enumerate(soup.find_all(["ul", "ol"])[:60]):
        if listing.find_parent(["ul", "ol", "table"]) is not None:
            continue
        items = listing.find_all("li", recursive=False)
        if len(items) < 3:
            continue
        parsed = []
        for item in items[:MAX_OBSERVATIONS]:
            anchor = next((a for a in item.find_all("a", href=True)
                           if not str(a.get("href", "")).strip().casefold().startswith(_SKIPPED_HREF)
                           and len(_clean(a.get_text(" ", strip=True)).split()) >= 2
                           and not is_generic_link_label(_clean(a.get_text(" ", strip=True)))), None)
            if anchor is None:
                continue
            title = _clean(anchor.get_text(" ", strip=True))
            address = urljoin(base, str(anchor.get("href")).strip())
            rest = _clean(item.get_text(" ", strip=True).replace(title, " ", 1)).strip(_LIST_SEPARATORS + " ")
            stamp = item.find("time")
            if stamp is not None and stamp.get("datetime"):
                rest = str(stamp.get("datetime")).strip()
            fields = {"title": title, _link_name(title, address, base): address}
            if rest:
                fields["date" if find_periods(rest) and len(rest.split()) <= 8 else "description"] = rest[:300]
            parsed.append(fields)
        if len(parsed) < 3 or len(parsed) < 0.8 * len(items):
            continue
        if sum(len(fields["title"].split()) for fields in parsed) / len(parsed) < 3:
            continue
        context, kind = _context_for(listing)
        rows.extend((fields, context, kind, f"list#{index + 1}") for fields in parsed)
    return rows


_SCALE = r"(?:\s?(?:trillion|billion|million|thousand|crore|lakh|bn|mn|tn|k|m)\b)?"
_QUANTITY = re.compile(
    # Numbers never end in a separator: "$27,500," yields "$27,500".
    r"(?:[$€£¥₹]|\b(?:usd|eur|gbp|inr|rs\.?|us\$)\s?)\s?\d(?:[\d,]*\d)?(?:\.\d+)?" + _SCALE + r"(?:\s?/\s?[a-z]{1,6})?"
    r"|[-+]?\d(?:[\d,]*\d)?(?:\.\d+)?\s?(?:%|percent\b|per cent\b)"
    r"|[-+]?\d(?:[\d,]*\d)?(?:\.\d+)?" + _SCALE + r"\s?(?:[a-z]{1,8}(?:\s?/\s?[a-z]{1,6})?)",
    re.I,
)
_YEAR_ONLY = re.compile(r"^(?:19|20)\d{2}$")
# Counters of page activity, not facts about the subject ("12,400 views").
_ENGAGEMENT = frozenset({
    "views", "likes", "reviews", "results", "followers", "comments", "shares", "subscribers",
    "downloads", "votes", "ratings", "replies", "retweets", "reactions", "clicks", "hits", "pages",
    "photos", "images", "videos", "posts", "articles", "stories", "items", "listings", "words",
    "characters", "visits", "impressions", "members", "questions", "answers",
})
# Plural nouns that do not end in "s".
_IRREGULAR_PLURALS = frozenset({"people", "children", "men", "women", "cattle", "staff", "personnel", "livestock"})
# Undated statements per page: the period is then inferred from the page dates.
MAX_UNDATED_STATEMENTS = 15

# What a quantity in prose is: a level ("reached $108/kWh"), a change ("down
# 8%", "$3,000 less", "a decline of $3") or a bound ("under $20,000"). Only
# levels are values of a measure; changes are recorded separately and bounds
# are ignored. The cues are ordinary English, independent of any subject.
_BOUND_BEFORE = re.compile(
    r"\b(?:under|over|below|above|less than|more than|fewer than|up to|at least|at most|"
    r"exceeding|beyond|upwards of|in excess of)\s*$", re.I)
_CHANGE_BEFORE = re.compile(
    r"\b(?:by|up|down|rose|fell|increased?|decreased?|dropped|climbed|jumped|declined|grew|gained|"
    r"rise|drop|jump|gain|grow|fall|climb|slip|slide|cut|saving|savings|difference of|(?:decline|increase|drop|rise|fall|reduction|jump|gain|growth|"
    r"change|premium|discount)s?\s+of(?:\s+(?:roughly|about|around|nearly|almost|some))?)\s*$", re.I)
_CHANGE_AFTER = re.compile(
    r"^\s*(?:less|more|lower|higher|cheaper|pricier|increase|decrease|drop|rise|gain|decline|jump|"
    r"year[- ]over[- ]year|yoy|annually)\b", re.I)


def _role(sentence: str, start: int, end: int, text: str) -> str:
    before = sentence[max(0, start - 40):start]
    after = sentence[end:end + 24]
    if _BOUND_BEFORE.search(before):
        return "bound"
    if _CHANGE_BEFORE.search(before) or _CHANGE_AFTER.search(after) or text.startswith(("+", "-")):
        return "change"
    return "level"


def _counted(sentence: str, number: str, number_end: int) -> str | None:
    """"1,280 hospitals": a count of something, stated as number + plural noun.

    Only counts of 1,000 or more qualify (smaller integers are ordinals, list
    positions and page furniture far more often), never engagement counters.
    """
    digits = number.replace(",", "")
    if "." in number or not digits.isdigit() or not 1000 <= int(digits) < 10_000_000:
        return None
    following = re.match(r"\s+([A-Za-z][a-z]{2,})\b", sentence[number_end:])
    if not following:
        return None
    noun = following.group(1).casefold()
    plural = noun in _IRREGULAR_PLURALS or (noun.endswith("s") and not noun.endswith("ss"))
    if not plural or noun in _ENGAGEMENT:
        return None
    return f"{number} {noun}"


def _quantities(sentence: str) -> list[tuple[int, str, str]]:
    """Quantities in a sentence as (position, text, role)."""
    found = []
    for match in _QUANTITY.finditer(sentence):
        text = match.group(0).strip()
        number = re.search(r"\d(?:[\d,]*\d)?(?:\.\d+)?", text).group(0)
        tail = text[text.find(number) + len(number):].strip().casefold()
        if _YEAR_ONLY.match(number) and not re.match(r"^[$€£¥₹]|^(?:usd|eur|gbp|inr|rs)", text, re.I):
            continue
        is_money = bool(re.match(r"^(?:[$€£¥₹]|usd|eur|gbp|inr|rs|us\$)", text, re.I))
        is_percent = "%" in text or "percent" in tail or "per cent" in tail
        unit_word = re.split(r"[\s/]", tail)[0] if tail else ""
        scaled = bool(re.search(r"trillion|billion|million|thousand|crore|lakh|\bbn\b|\bmn\b", tail))
        has_unit = unit_word.rstrip("s") in UNIT_TOKENS or "/" in tail
        counted = None
        if not (is_money or is_percent or scaled or has_unit):
            number_end = match.start() + match.group(0).find(number) + len(number)
            counted = _counted(sentence, number, number_end)
        if not (is_money or is_percent or scaled or has_unit or counted or "." in number):
            continue
        if counted:
            text = counted
        elif not is_money and not is_percent and not scaled and not has_unit:
            text = number
        role = _role(sentence, match.start(), match.end(), text)
        if is_percent and role == "level" and re.search(r"\bof\b", sentence[match.end():match.end() + 8]):
            role = "share"  # "81 percent of milk" is a share, not a level
        found.append((match.start(), text, role))
    return found


def _statement_rows(soup: BeautifulSoup) -> list[tuple[dict[str, str], str, str, str]]:
    """Sentences stating a quantity, as (fields, context, context kind, method).

    Dated sentences are ``text_statement``; a few undated ones ("Tokyo has
    13.96 million people") are ``undated_statement``, trusted less, with the
    period left to the scorer (inferred from the page dates).
    """
    root = soup.select_one("main, article, [role=main]") or soup.body or soup
    rows = []
    seen = set()
    undated = 0
    for node in root.find_all(["p", "li", "figcaption", "blockquote", "dd"])[:800]:
        if node.find_parent("table") is not None or node.find(["p", "li"]) is not None:
            continue
        text = _clean(node.get_text(" ", strip=True))
        if len(text) < 30:
            continue
        for sentence in re.split(r"(?<=[.!?;])\s+(?=[A-Z0-9“\"(])", text):
            sentence = sentence.strip()
            if not 25 <= len(sentence) <= 320 or sentence in seen:
                continue
            quantities = _quantities(sentence)
            levels = [item for item in quantities if item[2] == "level"]
            changes = [item for item in quantities if item[2] == "change"]
            if not levels and not changes:
                continue
            years = [(match.start(), match.group(0)) for match in re.finditer(r"(?<![\d$€£¥₹,.])(?:19|20)\d{2}(?!\d)", sentence)]
            periods = find_periods(sentence)
            if not periods:
                if levels and undated < MAX_UNDATED_STATEMENTS:
                    undated += 1
                    seen.add(sentence)
                    context, kind = _context_for(node)
                    rows.append(({"statement": sentence, "value": levels[0][1]}, context, kind, "undated_statement"))
                continue
            position, value, _ = (levels or changes)[0]
            if len(periods) == 1:
                period = periods[0].label
            elif years:
                # Several periods: take the one stated closest to the value,
                # preferring "in/during/for YYYY" phrasing.
                def distance(item: tuple[int, str]) -> tuple[int, int]:
                    before = sentence[max(0, item[0] - 7):item[0]].casefold()
                    preferred = 0 if re.search(r"\b(?:in|during|for|as of|by)\s$", before) else 1
                    return preferred, abs(item[0] - position)
                period = min(years, key=distance)[1]
            else:
                period = ""
            seen.add(sentence)
            context, kind = _context_for(node)
            # A sentence that only states a change records it as such, never as a level.
            record = {"statement": sentence, "value" if levels else "change": value}
            if levels and changes:
                record["change"] = changes[0][1]
            if period:
                record["period"] = period
            rows.append((record, context, kind, "text_statement"))
            if len(rows) >= 60:
                return rows
    return rows


def unpivot_wide(observation: Observation) -> list[Observation]:
    """Split a stored wide row whose keys are periods into one row per period.

    New pages are unpivoted while parsing; this repairs observations stored by
    earlier versions so they can be re-scored without re-fetching the page.
    """
    period_keys = [key for key in observation.fields if parse_period(key.replace("_", "/"))]
    if len(period_keys) < 3 or len(period_keys) < 0.5 * len(observation.fields):
        return [observation]
    labels = {("series" if key == "label" else key): value
              for key, value in observation.fields.items() if key not in period_keys}
    rows = []
    for key in period_keys:
        period = parse_period(key.replace("_", "/"))
        fields = {**labels, "period": period.label if period else key, "value": observation.fields[key]}
        rows.append(observation.model_copy(update={"fields": fields, "id": ""}))
    return [Observation.model_validate(row.model_dump()) for row in rows]


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

def _page_summary(soup: BeautifulSoup, result: ExecutionResult, provenance: dict[str, Any]) -> dict[str, str]:
    fields = {"title": provenance["title"] or _clean(result.title)}
    if provenance["heading"]:
        fields["heading"] = provenance["heading"]
    description = soup.select_one('meta[name="description"]')
    if description and description.get("content"):
        fields["description"] = _clean(description.get("content"))
    body = soup.select_one("main, article, body")
    if body:
        fields["text"] = _clean(body.get_text(" ", strip=True))
    return {key: value for key, value in fields.items() if value}


@dataclass(slots=True)
class ParsedPage:
    observations: list[Observation]
    dataset_links: list[DatasetLink]
    provenance: dict[str, Any]
    file_links: list[FoundFile] = field(default_factory=list)


def parse_rendered_page(result: ExecutionResult, plan: GoalPlan | None = None) -> list[Observation]:
    """Turn rendered HTML into raw observations with provenance attached.

    ``plan`` is accepted for interface symmetry; parsing is request-independent.
    """
    return parse_page(result).observations


def parse_page(result: ExecutionResult, *, site_adapters: bool = True) -> ParsedPage:
    """Observations, the datasets a page links to, and every file it links to.

    ``site_adapters`` enables the optional per-site extractors (``vora.extraction.sites``).
    """
    soup = BeautifulSoup(result.html, "html.parser")
    provenance = page_provenance(soup, result)
    body_text = (soup.body or soup).get_text(" ", strip=True)[:4000]

    gathered: list[tuple[str, dict[str, str], str, str, str, str]] = []
    links: list[DatasetLink] = []
    files: list[FoundFile] = []
    if is_challenge_page(provenance["title"], body_text) or \
            (len(body_text) < 1500 and is_login_wall(body_text)):
        gathered.append(("page_summary", _page_summary(soup, result, provenance), "", "", "challenge", ""))
    else:
        links = find_dataset_links(soup, result.final_url)
        files = find_file_links(soup, result.final_url)
        adapter = adapter_for(result.final_url) if site_adapters else None
        if adapter:
            domain, extract = adapter
            gathered += [("site_adapter", row, f"{domain} listing", "heading", "data", "site_adapter")
                         for row in extract(soup, result.final_url)[:MAX_OBSERVATIONS]]
        gathered += [("json_ld", row, "", "", "data", "json_ld") for row in _json_rows(soup)]
        gathered += [("hydration_json", row, "", "", "data", f"hydration:{label}")
                     for row, label in _hydration_rows(soup)]
        gathered += [("html_table", row, context, kind, "data", block)
                     for row, context, kind, block in _table_rows(soup, result.final_url)]
        _remove_noise(soup)
        gathered += [("repeated_region", row, context, kind, "data", block)
                     for row, context, kind, block in _card_rows(soup, result.final_url)]
        gathered += [("list_item", row, context, kind, "data", block)
                     for row, context, kind, block in _list_rows(soup, result.final_url)]
        # Sentences are judged one by one, never as a block.
        gathered += [(method, row, context, kind, "data", "")
                     for row, context, kind, method in _statement_rows(soup)]
        if not gathered:
            summary = _page_summary(soup, result, provenance)
            # Preserve any content the engine actually received; scoring decides
            # later whether it is useful.
            if any(summary.values()):
                gathered.append(("page_summary", summary, "", "", "data", ""))

    return ParsedPage(_observations(gathered, result, provenance), links, provenance, files)


def dataset_observations(rows: list[dict[str, str]], link: DatasetLink | str, *, title: str, context: str,
                         modified_at: str | None, fetched_at: str, method: str = "linked_dataset",
                         published_at: str | None = None) -> list[Observation]:
    """Observations from a downloaded file, with the file as their source."""
    url = link if isinstance(link, str) else link.url
    observations = []
    for fields in rows:
        fields = {_key(key): value for key, value in fields.items() if key and value and _key(key)}
        if not fields:
            continue
        observations.append(Observation(
            source_url=url, method=method, fields=fields,
            source_domain=urlparse(url).netloc.removeprefix("www."),
            source_title=title, published_at=published_at, modified_at=modified_at, fetched_at=fetched_at,
            extraction_confidence=METHOD_CONFIDENCE.get(method, 0.8),
            context=context[:240], context_kind="caption" if context else "",
        ))
    return observations


def _observations(gathered: Iterable[tuple[str, dict[str, str], str, str, str, str]],
                  result: ExecutionResult, provenance: dict[str, Any]) -> list[Observation]:
    fetched_at = provenance["fetched_at"]
    # States of an interactively explored page ("tab: 2023") keep their blocks apart.
    state = str(result.metadata.get("state") or "") if result.metadata else ""
    prefix = f"{state}/" if state else ""
    seen = set()
    observations = []
    for method, fields, context, context_kind, role, block in gathered:
        fields = {_key(key): value for key, value in fields.items() if key and value and _key(key)}
        if not fields:
            continue
        signature = json.dumps(fields, sort_keys=True, ensure_ascii=False).casefold()
        if signature in seen:
            continue
        seen.add(signature)
        verdict = assess(fields, method, role)
        observations.append(Observation(
            source_url=result.final_url,
            method=method,
            fields=fields,
            content_role=verdict.role,
            noise_probability=verdict.probability,
            reasons=[verdict.reason] if verdict.reason else [],
            source_domain=provenance["domain"],
            source_title=provenance["title"] or provenance["site_name"],
            published_at=provenance["published_at"],
            modified_at=provenance["modified_at"],
            fetched_at=fetched_at,
            extraction_confidence=METHOD_CONFIDENCE.get(method, 0.5),
            context=context,
            context_kind=context_kind,
            temporal_coverage=provenance["temporal_coverage"],
            block_id=f"{prefix}{block}" if block else "",
        ))
        if len(observations) >= MAX_OBSERVATIONS:
            break
    return observations
