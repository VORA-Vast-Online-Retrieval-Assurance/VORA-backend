"""Site recipes: a declarative, replayable way to read one site's listing.

A recipe says how to reach a listing (a few clicks), which table holds it, what each column means,
which column is the record's own identifier, how to page through it, and which links can be built
from the identifier. One runner executes every recipe; recipes are data (``data/sources.json``).

A recipe is checked every time it runs (a dry run on its first page): if the table is missing or
its identifiers no longer look right, the run reports that the site changed and the caller falls back
to the general reader, so a stale recipe never fills the dataset with wrong rows.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urljoin, urlparse

from pydantic import BaseModel, Field, field_validator, model_validator
from urllib.parse import parse_qsl

if TYPE_CHECKING:
    from vora.shared.contracts import Observation

    from vora.browser.engine import BrowserEngine


def _plain(value: str | None) -> str | None:
    """Selectors and labels come from files and from pages: bounded, single line, no control characters."""
    if value is None:
        return None
    if len(value) > 400 or any(ord(ch) < 32 for ch in value):
        raise ValueError("a selector or label is too long or has control characters")
    return value


class Step(BaseModel):
    """Click one element: by CSS selector, or a link/button whose text contains ``click_text``."""

    click: str | None = None
    click_text: str | None = None
    optional: bool = True
    navigates: bool = False

    _bounded = field_validator("click", "click_text")(_plain)


class Pagination(BaseModel):
    type: Literal["postback", "next_link", "load_more", "scroll", "none"] = "none"
    # For load_more: the control that adds rows.
    control: str | None = Field(default=None, max_length=400)
    # For postback: the argument prefix in the pager links ("Page$" in __doPostBack('grid','Page$2')).
    pattern: str = "Page$"
    max_pages: int = Field(5, ge=1, le=50)


class LinkRule(BaseModel):
    """Build a link from a field: ``template`` with {1}, {2}... filled from ``pattern``'s groups."""

    source: str = Field(alias="from")
    pattern: str
    template: str = Field(pattern=r"^https://")

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        re.compile(value)
        return value


class Recipe(BaseModel):
    """How to read one listing: ``table`` (rows with named columns) or ``link_list`` (items that are one link each,
    identified by a number in their address)."""

    kind: Literal["table", "link_list", "json_api"] = "table"
    ready_steps: list[Step] = Field(default_factory=list, max_length=12)
    table: str | None = None
    columns: dict[str, str] = Field(default_factory=dict, max_length=60)
    id_field: str = "id"
    id_pattern: str = ".+"
    # Where a table row's identity comes from: a column's text, an attribute of the row element, or a parameter of a
    # link in the row. Chosen by the learner from what the site itself uses (see vora.learning.structure.id_candidates).
    id_source: Literal["column", "attr", "param"] = "column"
    id_name: str | None = Field(default=None, max_length=80)
    pagination: Pagination = Field(default_factory=Pagination)
    links: dict[str, LinkRule] = Field(default_factory=dict, max_length=8)
    # link_list: the box holding the items, what one item is, and the address parts that identify it.
    container: str | None = None
    item: str = Field("tr", max_length=160)               # css of one item, relative to the container
    id_params: list[str] = Field(default_factory=list, max_length=12)
    # json_api: the paged data service behind a listing (see vora/learning/api_records.py).
    api_url: str | None = Field(default=None, pattern=r"^https?://", max_length=1500)
    records_path: list[str] = Field(default_factory=list, max_length=8)
    id_fields: list[str] = Field(default_factory=list, max_length=6)
    id_fields_link: list[str] = Field(default_factory=list, max_length=6)
    link_template: str | None = Field(default=None, max_length=600)
    page_param: str | None = Field(default=None, max_length=40)
    page_start: int = Field(1, ge=0, le=1_000_000)
    page_offset: bool = False
    size_param: str | None = Field(default=None, max_length=40)
    api_size: int | None = Field(default=None, ge=1, le=500)
    total_pages_path: list[str] = Field(default_factory=list, max_length=8)
    next_path: list[str] = Field(default_factory=list, max_length=8)      # where the response names the next page

    _bounded = field_validator("table", "container", "page_param", "size_param", "link_template", "item", "id_name")(_plain)

    @model_validator(mode="after")
    def _complete(self) -> "Recipe":
        if self.kind == "table" and (not self.table or not self.columns):
            raise ValueError("a table recipe needs a table and its columns")
        if self.kind == "json_api" and (not self.api_url or not self.id_fields):
            raise ValueError("a json_api recipe needs the data address and the fields that identify a record")
        if self.kind == "link_list" and not self.container:
            raise ValueError("a link-list recipe needs a container")
        return self

    @field_validator("id_pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        re.compile(value)
        return value


@dataclass(slots=True)
class RecipeRun:
    ok: bool = False
    rows: list["Observation"] = field(default_factory=list)
    pages: int = 0
    title: str = ""
    final_url: str = ""
    note: str = ""
    stopped_at_known: bool = False


# Reads the recipe's table: header cells, then each row's cell texts and first link.
_READ_TABLE = r"""
(selector) => {
  const table = document.querySelector(selector);
  if (!table) return null;
  // a <table>, or an element with grid/table roles
  const attrsOf = (el) => { const out = {};
    for (const a of el.attributes) if ((a.name === 'id' || a.name.startsWith('data-')) && a.value && a.value.length <= 120) out[a.name] = a.value;
    return out; };
  const rows = table.tagName === 'TABLE'
    ? [...table.rows].map((row) => ({el: row, cells: [...row.cells], header: !!row.querySelector('th')}))
    : [...table.querySelectorAll('[role=row]')].map((row) => ({
        el: row, cells: [...row.querySelectorAll('[role=gridcell],[role=cell],[role=columnheader],[role=rowheader]')],
        header: !!row.querySelector('[role=columnheader]')}));
  const headerRow = rows.find((row) => row.header) || rows[0];
  if (!headerRow) return {headers: [], rows: []};
  const headers = headerRow.cells.map((cell) => cell.innerText.replace(/\s+/g, ' ').trim());
  const body = rows.filter((row) => row !== headerRow && !row.header && row.cells.length === headerRow.cells.length);
  return {headers, row_attrs: body.map((row) => attrsOf(row.el)), rows: body.map((row) => row.cells.map((cell) => {
    const link = cell.querySelector('a[href]');
    const href = link ? link.getAttribute('href') : '';
    return {text: cell.innerText.replace(/\s+/g, ' ').trim(),
            href: href && !href.startsWith('javascript:') ? link.href : ''};
  }))};
}
"""

# A session id kept in the path by some servers ("/(S(abc123))/page.aspx") changes on every visit.
_SESSION_SEGMENT = re.compile(r"/\(S\([^)]*\)\)", re.I)


def canonical_url(url: str) -> str:
    return _SESSION_SEGMENT.sub("", url)


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def build_links(fields: dict[str, str], recipe: Recipe) -> dict[str, str]:
    """Links the recipe builds from a row's own values (e.g. a PDF from its identifier)."""
    built = {}
    for name, rule in recipe.links.items():
        match = re.search(rule.pattern, fields.get(rule.source, ""))
        if match:
            url = rule.template
            for index, group in enumerate(match.groups(), start=1):
                url = url.replace(f"{{{index}}}", group or "")
            built[name] = url
    return built


def href_params(href: str) -> dict[str, str]:
    """The query parameters of an address, and its last path segment (as "path") when that looks like a key."""
    parts = urlparse(href)
    found = {key.lower(): value for key, value in parse_qsl(parts.query) if value}
    tail = re.search(r"/([A-Za-z0-9_-]{4,})/?$", parts.path)
    if tail:
        found.setdefault("path", tail.group(1))
    return found


def row_identity(recipe: Recipe, cells: list[dict], attributes: dict | None) -> str:
    """A row's identity when it is not a column: an attribute of the row, or a parameter of a link in it."""
    if recipe.id_source == "attr":
        return str((attributes or {}).get(recipe.id_name or "", ""))
    for cell in cells:
        if cell.get("href"):
            value = href_params(cell["href"]).get((recipe.id_name or "").lower())
            if value:
                return value
    return ""


def rows_from_table(table: dict | None, recipe: Recipe) -> tuple[list[dict[str, str]], str]:
    """Map a read table to fields. Returns the rows and, when the table does not fit the recipe, why."""
    if not table:
        return [], f"table {recipe.table} not found"
    wanted = {_key(header): name for header, name in recipe.columns.items()}
    positions = {index: wanted[_key(header)] for index, header in enumerate(table["headers"]) if _key(header) in wanted}
    if recipe.id_source == "column" and recipe.id_field not in positions.values():
        return [], f"no '{recipe.id_field}' column among {table['headers'][:10]}"
    id_pattern = re.compile(recipe.id_pattern)
    rows = []
    attributes = table.get("row_attrs") or []
    for number, cells in enumerate(table["rows"]):
        fields: dict[str, str] = {}
        if recipe.id_source != "column":
            fields[recipe.id_field] = row_identity(recipe, cells, attributes[number] if number < len(attributes) else None)
        for index, name in positions.items():
            if index < len(cells):
                fields[name] = cells[index]["text"]
                if cells[index]["href"]:
                    fields[f"{name}_url"] = cells[index]["href"]
        if not id_pattern.match(fields.get(recipe.id_field, "")):
            continue
        fields.update(build_links(fields, recipe))
        rows.append(fields)
    if table["rows"] and not rows:
        return [], f"identifiers no longer match {recipe.id_pattern}"
    return rows, ""


def numeric_params(url: str) -> dict[str, str]:
    """The numeric query parameters of an address (and a long numeric path segment, as "path")."""
    parts = urlparse(url)
    found = {key.lower(): value for key, value in parse_qsl(parts.query) if value.isdigit()}
    tail = re.search(r"/(\d{3,})(?:/|\.|$)", parts.path)
    if tail:
        found.setdefault("path", tail.group(1))
    return found


def section_of(url: str) -> str:
    """The page an address points at ('.../BS_PressReleaseDisplay.aspx?prid=1' -> 'bs_pressreleasedisplay')."""
    last = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"\.[A-Za-z0-9]{1,5}$", "", last).lower()


def item_fields(item: dict, params: list[str]) -> dict[str, str]:
    """A link item as a record: its title, address, the page type, and the number that identifies it."""
    fields = {"title": item["title"], "url": item["href"], "section": section_of(item["href"])}
    if item.get("text") and item["text"] != item["title"]:
        fields["text"] = item["text"][:400]
    numbers = numeric_params(item["href"])
    for key in params:
        if key in numbers:
            fields["id"] = f"{key}={numbers[key]}"
            break
    else:
        fields["id"] = "url=" + canonical_url(item["href"].split("#")[0])      # a slug, a hash, a text key
    return fields


# Reads a collection of linked items: each item of the container, with its most descriptive link.
_READ_LIST = r"""
([selector, item]) => {
  const box = document.querySelector(selector);
  if (!box) return null;
  const items = item === 'tr' ? [...box.querySelectorAll(':scope > tbody > tr, :scope > tr')]
                              : [...box.querySelectorAll(':scope > ' + item)];
  const label = (a) => (a.innerText || a.getAttribute('aria-label') || a.title || '').replace(/\s+/g, ' ').trim();
  return items.map((el) => {
    const links = [...el.querySelectorAll('a[href]')].filter((a) => !/^(javascript:|#)/.test(a.getAttribute('href') || ''));
    if (!links.length || links.length > 8) return null;
    const best = links.reduce((x, y) => (label(y).length > label(x).length ? y : x));
    return {title: label(best), href: best.href, text: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 400)};
  }).filter(Boolean);
}
"""
_MORE_LINK = re.compile(r"\bmore\b|\bview all\b|\bsee all\b", re.I)


def natural_id(domain: str, label: str, identifier: str) -> str:
    """The same record, however and whenever it was read, gets the same id (the format
    ``Observation`` uses for rows that carry an identifier, so the general reader agrees)."""
    import json

    payload = json.dumps([domain, f"{label}={identifier.strip().upper()}"], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def goto(page: Any, url: str, timeout_ms: int) -> None:
    """Open ``url``; a site that redirects itself (a session address, a cookie notice) is followed, not an error."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:  # noqa: BLE001
        if "interrupted by another navigation" not in str(exc):
            raise
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)


def _wait_for(page: Any, selector: str | None, seconds: float = 12) -> None:
    """Give the page a moment to draw the listing after a click or a postback."""
    if not selector:
        return
    try:
        page.wait_for_selector(selector, state="attached", timeout=int(seconds * 1000))
    except Exception:  # noqa: BLE001 - reported by the read that follows ("not found")
        pass


def _evaluate(page: Any, script: str, argument: Any) -> Any:
    """page.evaluate, tried again once when the page navigated under it."""
    try:
        return page.evaluate(script, argument)
    except Exception as exc:  # noqa: BLE001
        if "context was destroyed" not in str(exc) and "navigation" not in str(exc):
            raise
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:  # noqa: BLE001
            pass
        return page.evaluate(script, argument)


def _click(page: Any, step: Step, timeout_ms: int) -> bool:
    if step.click:
        locator = page.locator(step.click)
    else:
        text = (step.click_text or "").replace('"', '\\"')
        locator = page.locator(f'a:has-text("{text}"), button:has-text("{text}"), input[value*="{text}"]')
    try:
        if locator.count() == 0 or not locator.first.is_visible():
            return False
        if step.navigates:
            with page.expect_navigation(timeout=timeout_ms):
                locator.first.click(timeout=timeout_ms)
        else:
            locator.first.click(timeout=timeout_ms)
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        return True
    except Exception:
        return False


def _next_page(page: Any, recipe: Recipe, number: int, first_id: str, timeout_ms: int) -> bool:
    """Move to page ``number``; true once the table shows different rows."""
    pagination = recipe.pagination
    if pagination.type in {"load_more", "scroll"}:
        return _grow(page, recipe, timeout_ms)
    if pagination.type == "postback":
        locator = page.locator(f'a[href*="{pagination.pattern}{number}\'"]')
    elif pagination.type == "next_link":
        locator = page.locator('a[rel=next], a:has-text("Next"), a:has-text("›"), a:has-text("»"), '
                               'button:has-text("Next"), [role=button]:has-text("Next"), [aria-label*="next" i]')
    else:
        return False
    try:
        if locator.count() == 0:
            return False
        try:
            with page.expect_navigation(timeout=timeout_ms):
                locator.first.click(timeout=timeout_ms)
        except Exception:
            pass  # an update panel refreshes in place
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            table = page.evaluate(_READ_TABLE, recipe.table)
            rows, _ = rows_from_table(table, recipe)
            if rows and rows[0].get(recipe.id_field) != first_id:
                return True
            time.sleep(0.3)
    except Exception:
        return False
    return False


def _row_count(page: Any, recipe: Recipe) -> int:
    rows, _ = rows_from_table(_evaluate(page, _READ_TABLE, recipe.table), recipe)
    return len(rows)


def _grow(page: Any, recipe: Recipe, timeout_ms: int) -> bool:
    """A listing that lengthens in place: press its load-more control, or scroll to the end; true once it has more rows."""
    try:
        before = _row_count(page, recipe)
        if recipe.pagination.type == "load_more":
            control = page.locator(recipe.pagination.control or "")
            if control.count() == 0 or not control.first.is_visible():
                return False
            control.first.click(timeout=timeout_ms)
        else:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        deadline = time.monotonic() + min(timeout_ms / 1000, 12)
        while time.monotonic() < deadline:
            if _row_count(page, recipe) > before:
                return True
            time.sleep(0.4)
    except Exception:  # noqa: BLE001
        return False
    return False


def _run_list(engine: "BrowserEngine", url: str, recipe: Recipe, *, source_name: str = "",
              known_ids: set[str] | None = None) -> RecipeRun:
    """Read a link collection: open the page, follow the recipe's clicks, take every titled link of the container."""
    from vora.shared.contracts import Observation

    from vora.browser.engine import new_context

    run = RecipeRun()
    timeout_ms = engine.settings.navigation_timeout_ms
    context = new_context(engine)
    try:
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        goto(page, url, timeout_ms)
        for step in recipe.ready_steps:
            if not _click(page, step, timeout_ms) and not step.optional:
                run.note = f"step '{step.click or step.click_text}' not found"
                return run
        _wait_for(page, recipe.container)
        items = _evaluate(page, _READ_LIST, [recipe.container, recipe.item])
        if items is None:
            run.note = f"container {recipe.container} not found"
            return run
        domain = (urlparse(url).hostname or "").removeprefix("www.")
        page_url = canonical_url(page.url)
        fetched_at = datetime.now(UTC)
        seen: set[str] = set()
        for item in items[:500]:
            if len(item["title"]) < 40 and _MORE_LINK.search(item["title"]):
                continue                      # the "view more" link is not a record
            fields = item_fields(item, recipe.id_params)
            identifier = fields.get("id")
            if identifier is None or identifier in seen:
                continue
            seen.add(identifier)
            run.rows.append(Observation(
                id=natural_id(domain, "id", identifier), source_url=page_url, method="recipe", fields=fields,
                source_title=page.title(), extraction_confidence=0.9, context=source_name, context_kind="caption",
                block_id=f"recipe:{recipe.container}", fetched_at=fetched_at))
        run.pages, run.ok = 1, bool(run.rows)
        run.title, run.final_url = page.title(), page_url
        if not run.rows:
            run.note = "the container held no identifiable items"
        elif known_ids and all(row.fields["id"] in known_ids for row in run.rows):
            run.stopped_at_known = True
    finally:
        context.close()
    return run


def run_recipe(engine: "BrowserEngine", url: str, recipe: Recipe, *, source_name: str = "",
               known_ids: set[str] | None = None, max_pages: int | None = None,
               deadline: float | None = None) -> RecipeRun:
    """Open ``url``, follow the recipe, and return one observation per record found."""
    from vora.shared.contracts import Observation

    from vora.browser.engine import new_context

    if recipe.kind == "json_api":
        from vora.learning.api_records import run_api

        return run_api(engine, url, recipe, source_name=source_name, known_ids=known_ids, max_pages=max_pages,
                       deadline=deadline)
    if recipe.kind == "link_list":
        return _run_list(engine, url, recipe, source_name=source_name, known_ids=known_ids)
    known = known_ids or set()
    pages_wanted = min(max_pages or recipe.pagination.max_pages, recipe.pagination.max_pages)
    timeout_ms = engine.settings.navigation_timeout_ms
    run = RecipeRun()
    context = new_context(engine)
    try:
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        goto(page, url, timeout_ms)
        for step in recipe.ready_steps:
            if not _click(page, step, timeout_ms) and not step.optional:
                run.note = f"step '{step.click or step.click_text}' not found"
                break
        _wait_for(page, recipe.table)
        seen: set[str] = set()
        fetched_at = datetime.now(UTC)
        domain = (urlparse(url).hostname or "").removeprefix("www.")
        while True:
            table = _evaluate(page, _READ_TABLE, recipe.table)
            rows, problem = rows_from_table(table, recipe)
            if problem:
                run.note = run.note or problem
                break
            run.pages += 1
            page_url = canonical_url(page.url)
            new_on_page = 0
            for fields in rows:
                identifier = fields[recipe.id_field]
                if identifier in seen:
                    continue
                seen.add(identifier)
                new_on_page += identifier not in known
                for name, value in list(fields.items()):
                    if name.endswith("_url") and value and not value.startswith("http"):
                        fields[name] = urljoin(page_url, value)
                run.rows.append(Observation(
                    id=natural_id(domain, recipe.id_field, identifier), source_url=page_url, method="recipe", fields=fields,
                    source_title=page.title(), extraction_confidence=0.95, context=source_name,
                    context_kind="caption", block_id=f"recipe:{recipe.table}", fetched_at=fetched_at,
                ))
            run.ok = True
            if rows and new_on_page == 0 and known:
                run.stopped_at_known = True  # everything on this page was read before
                break
            if run.pages >= pages_wanted or (deadline is not None and time.monotonic() >= deadline):
                break
            if not rows or not _next_page(page, recipe, run.pages + 1, rows[0][recipe.id_field], timeout_ms):
                break
        run.title = page.title()
        run.final_url = canonical_url(page.url)
    finally:
        context.close()
    return run
