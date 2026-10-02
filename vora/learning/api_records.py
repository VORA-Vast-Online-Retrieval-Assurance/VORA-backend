"""Records a page loads from its own data service.

Many modern sites draw their listing from a paged JSON service (``.../search?page=1&size=10`` answering
``{"records": [...], "totalPages": 689}``) and the table on screen is only its first page. Reading the service
directly is faster, complete and independent of the layout. This module has both halves:

* ``learn_api`` opens a page, watches the data requests it makes, picks the one that returns a list of records, works
  out which query parameters page through it, how a record is identified and how a record links to its own page, and
  checks the result by reading two pages.
* ``run_api`` replays that: one public GET per page (no browser), records flattened to fields, stopping at records
  already collected.

Nothing here names a site, a parameter value or a field: names are found by their role (a number that changes by one
per page, a value that is the same in a page's link and in its record).
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

if TYPE_CHECKING:
    from vora.shared.contracts import Observation

    from vora.browser.engine import BrowserEngine
    from vora.learning.recipes import Recipe, RecipeRun

PAGE_PARAM = re.compile(r"^(page|pageno|page_no|pagenumber|page_number|pageindex|page_index|p|pg)$", re.I)
OFFSET_PARAM = re.compile(r"^(offset|start|skip|from)$", re.I)
SIZE_PARAM = re.compile(r"^(size|limit|pagesize|page_size|perpage|per_page|rows|count|take|max|length)$", re.I)
MAX_BODY = 3_000_000
MAX_FIELDS = 40
MAX_VALUE = 2000


def snake(name: str) -> str:
    """'debateTitle' -> 'debate_title'; anything that is not a word character becomes an underscore."""
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name))
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:60] or "field"


def dig(value: Any, path: list[str]) -> Any:
    for key in path:
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return None
    return value


def flatten(record: dict, prefix: str = "") -> dict[str, str]:
    """One level of readable fields: scalars as text, lists of scalars joined, nested objects with a prefix."""
    fields: dict[str, str] = {}
    for key, value in record.items():
        name = snake(f"{prefix}_{key}" if prefix else key)
        if isinstance(value, dict):
            if not prefix:
                fields.update(flatten(value, name))
        elif isinstance(value, list):
            scalars = [str(v) for v in value if isinstance(v, (str, int, float)) and not isinstance(v, bool)]
            if scalars:
                fields[name] = "; ".join(scalars)[:MAX_VALUE]
        elif value is not None and value != "":
            fields[name] = str(value)[:MAX_VALUE]
        if len(fields) >= MAX_FIELDS:
            break
    return fields


# ---------------------------------------------------------------------------------------------- finding records

def find_records(payload: Any, path: list[str] | None = None, depth: int = 0) -> tuple[list[str], list[dict]] | None:
    """The biggest list of similar objects in a JSON document: (path to it, the list)."""
    path = path or []
    best: tuple[list[str], list[dict]] | None = None
    if isinstance(payload, list):
        items = [item for item in payload if isinstance(item, dict)]
        if len(items) >= 3 and len(items) >= 0.9 * len(payload):
            keys = [set(item) for item in items]
            common = set.intersection(*keys)
            if len(common) >= 2:
                best = (path, items)
    elif isinstance(payload, dict) and depth < 4:
        for key, value in payload.items():
            found = find_records(value, [*path, key], depth + 1)
            if found and (best is None or _weight(found[1]) > _weight(best[1])):
                best = found
    return best


def _weight(items: list[dict]) -> int:
    return len(items) * len(set.intersection(*(set(item) for item in items)))


NEXT_KEY = re.compile(r"^(next|next_?page|next_?url|next_?link|nextpageurl)$", re.I)


def next_path_of(payload: Any, path: list[str] | None = None, depth: int = 0) -> list[str] | None:
    """Where a response names the address of its next page (cursor-style services answer with it)."""
    path = path or []
    if isinstance(payload, dict) and depth < 3:
        for key, value in payload.items():
            if NEXT_KEY.match(str(key)) and isinstance(value, str) and re.match(r"^(https?://|/)", value):
                return [*path, key]
            if isinstance(value, dict):
                found = next_path_of(value, [*path, key], depth + 1)
                if found:
                    return found
    return None


def paging_of(url: str) -> dict | None:
    """Which query parameters page through a data URL: {"page": name, "start": n, "step": 1|size, "size": name}."""
    params = dict(parse_qsl(urlparse(url).query, keep_blank_values=True))
    found: dict[str, Any] = {}
    for name, value in params.items():
        if PAGE_PARAM.match(name) and value.lstrip("-").isdigit():
            found.update(page=name, start=int(value), offset=False)
        elif OFFSET_PARAM.match(name) and value.lstrip("-").isdigit() and "page" not in found:
            found.update(page=name, start=int(value), offset=True)
        elif SIZE_PARAM.match(name) and value.isdigit():
            found.update(size=name, size_value=int(value))
    return found if "page" in found else None


def identify(items: list[dict], link_fields: list[str] | None = None) -> list[str]:
    """The record keys that identify a record: the ones its page link is built from, else the smallest set of
    scalar keys that is unique within the page (keys that look like identifiers first)."""
    scalars = [key for key in items[0] if all(isinstance(item.get(key), (str, int)) and item.get(key) != "" for item in items)]
    if link_fields and all(key in scalars for key in link_fields):
        return link_fields
    def rank(key: str) -> tuple:
        row_number = bool(re.search(r"serial|row|index|rank|sr_?no|s_?no$|position", snake(key)))   # shifts when rows are added
        return (1 if row_number else 0,
                0 if re.search(r"(^|_)id$", snake(key)) else 1,
                0 if re.search(r"(^|_)(id|slno|code|number|no|key)$", snake(key)) else 1,
                0 if all(isinstance(i[key], int) for i in items) else 1)

    ranked = sorted(scalars, key=rank)
    for key in ranked:
        if len({str(item[key]) for item in items}) == len(items):
            return [key]
    numbers = [key for key in ranked if all(isinstance(item[key], int) for item in items)]
    for a in numbers:
        for b in numbers:
            if a < b and len({(item[a], item[b]) for item in items}) == len(items):
                return [a, b]
    return []


def link_template(hrefs: list[str], record: dict, base: str) -> tuple[str, list[str]] | None:
    """How a record's own page address is built: an address on the page whose query numbers all appear in the record."""
    flat = {key: str(value) for key, value in record.items() if isinstance(value, (str, int)) and value != ""}
    for href in hrefs:
        parts = urlparse(urljoin(base, href))
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        numeric = [(name, value) for name, value in pairs if value.isdigit()]
        if not numeric:
            continue
        mapping, used = {}, []
        for name, value in numeric:
            keys = [key for key, text in flat.items() if text == value]
            if not keys:
                break
            mapping[name] = keys[0]
            used.append(keys[0])
        else:
            query = urlencode([(name, "{" + mapping[name] + "}" if name in mapping else value) for name, value in pairs],
                              safe="{}")
            return urlunparse(parts._replace(query=query)), used
    return None


def page_url(recipe: "Recipe", number: int) -> str:
    parts = urlparse(recipe.api_url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    size = recipe.api_size or 0
    params[recipe.page_param] = str(recipe.page_start + number * (size if recipe.page_offset else 1))
    if recipe.size_param and size:
        params[recipe.size_param] = str(size)
    return urlunparse(parts._replace(query=urlencode(params)))


def get_json(engine: "BrowserEngine", url: str) -> Any:
    """GET a data URL (public addresses only) and parse it; None when it is not JSON."""
    from vora.shared.urls import safe_get

    from vora.learning.structure import _plain_get

    fetch = safe_get if engine.settings.guard_network else _plain_get
    _, status, text = fetch(url, timeout=30, max_bytes=MAX_BODY, headers={"Accept": "application/json"})
    if status != 200:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------------------------- reading

def run_api(engine: "BrowserEngine", url: str, recipe: "Recipe", *, source_name: str = "",
            known_ids: set[str] | None = None, max_pages: int | None = None,
            deadline: float | None = None) -> "RecipeRun":
    from vora.shared.contracts import Observation

    from vora.learning.recipes import RecipeRun, canonical_url, natural_id

    run = RecipeRun()
    domain = (urlparse(recipe.api_url).hostname or "").removeprefix("www.")
    known = known_ids or set()
    wanted = min(max_pages or recipe.pagination.max_pages, recipe.pagination.max_pages)
    fetched_at = datetime.now(UTC)
    seen: set[str] = set()
    following = recipe.api_url
    for number in range(wanted):
        if deadline is not None and time.monotonic() >= deadline:
            run.note = "time budget reached"
            break
        try:
            payload = get_json(engine, following if recipe.next_path else page_url(recipe, number))
        except (ValueError, OSError) as exc:
            run.note = f"the data service did not answer ({type(exc).__name__})"
            break
        items = dig(payload, recipe.records_path) if recipe.records_path else payload
        if not isinstance(items, list) or not items:
            run.note = run.note or ("the data service returned no records" if number == 0 else "")
            break
        fresh = 0
        for item in items[:1000]:
            if not isinstance(item, dict):
                continue
            ident = "|".join(f"{key}={item.get(key)}" for key in recipe.id_fields)
            if not recipe.id_fields or any(item.get(key) in (None, "") for key in recipe.id_fields) or ident in seen:
                continue
            seen.add(ident)
            fields = {"id": ident, **flatten(item)}
            if recipe.link_template:
                try:
                    fields["url"] = urljoin(url, recipe.link_template.format(**{k: item[k] for k in recipe.id_fields_link}))
                except (KeyError, IndexError):
                    pass
            run.rows.append(Observation(
                id=natural_id(domain, "id", ident), source_url=canonical_url(fields.get("url") or url), method="recipe",
                fields=fields, source_title=source_name, extraction_confidence=0.95, context=source_name,
                context_kind="caption", block_id="recipe:api", fetched_at=fetched_at))
            fresh += ident not in known
        run.pages += 1
        if recipe.next_path:
            following = dig(payload, recipe.next_path)
            if not isinstance(following, str) or not following:
                break
            following = urljoin(recipe.api_url, following)
        total = dig(payload, recipe.total_pages_path) if recipe.total_pages_path else None
        if isinstance(total, int) and number + 1 >= total:
            break
        if known and fresh == 0:
            run.stopped_at_known = True
            break
    run.ok = bool(run.rows)
    run.final_url, run.title = url, source_name
    if not run.rows and not run.note:
        run.note = "no identifiable records"
    return run


# ---------------------------------------------------------------------------------------------- learning

def learn_api(engine: "BrowserEngine", url: str, notes: list[str], budget: float = 60) -> dict | None:
    """A ``json_api`` recipe for the paged data service behind ``url``'s listing, or None."""
    from vora.browser.engine import new_context
    from vora.learning.recipes import goto

    captured: list[tuple[str, Any]] = []
    context = new_context(engine)
    started = time.monotonic()
    try:
        page = context.new_page()
        page.set_default_timeout(engine.settings.navigation_timeout_ms)

        def on_response(response) -> None:
            try:
                if response.request.method != "GET" or response.request.resource_type not in {"xhr", "fetch"}:
                    return
                if response.status != 200 or "json" not in response.headers.get("content-type", ""):
                    return
                body = response.text()
                if len(body) <= MAX_BODY:
                    captured.append((response.url, json.loads(body)))
            except Exception:  # noqa: BLE001 - a response that cannot be read is not a candidate
                pass

        page.on("response", on_response)
        try:
            goto(page, url, engine.settings.navigation_timeout_ms)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"data service: could not open the page ({type(exc).__name__})")
            return None
        deadline = started + min(budget, 20)
        while time.monotonic() < deadline and not any(find_records(body) for _, body in captured):
            page.wait_for_timeout(1000)
        page.wait_for_timeout(1500)
        hrefs = page.eval_on_selector_all("a[href]", "els => els.map(a => a.getAttribute('href'))")
        page_address = page.url
    finally:
        context.close()

    candidates = []
    for address, body in captured:
        found = find_records(body)
        if not found:
            continue
        paging = paging_of(address)
        candidates.append(((1 if paging or next_path_of(body) else 0, _weight(found[1])), address, body, found, paging))
    if not candidates:
        notes.append("data service: the page made no request that returns a list of records")
        return None
    _, address, body, (path, items), paging = max(candidates, key=lambda c: c[0])
    template = link_template(hrefs, items[0], page_address)
    link_fields = template[1] if template else []
    id_fields = identify(items, link_fields)
    if not id_fields:
        notes.append("data service: no field identifies a record")
        return None
    following = next_path_of(body)
    recipe: dict[str, Any] = {
        "kind": "json_api", "api_url": address, "records_path": path, "id_fields": id_fields,
        "pagination": {"type": "none", "max_pages": 50 if paging or following else 1},
    }
    if following and not paging:
        recipe["next_path"] = following
    if template:
        recipe["link_template"], recipe["id_fields_link"] = template[0], template[1]
    if paging:
        recipe.update(page_param=paging["page"], page_start=paging["start"], page_offset=paging["offset"],
                      size_param=paging.get("size"), api_size=paging.get("size_value"))
        for key in ("totalPages", "total_pages", "pages", "pageCount"):
            for container in ([], *( [[k] for k in body if isinstance(body, dict) and isinstance(body[k], dict)] )):
                if isinstance(dig(body, [*container, key]), int):
                    recipe["total_pages_path"] = [*container, key]
        recipe = _widen(engine, recipe, notes)
        recipe = _larger_pages(engine, recipe, len(items), notes)
    notes.append(f"data service: {address.split('?')[0]} returns {len(items)} records per request"
                 + (f", paged by '{paging['page']}'" if paging else ""))
    return recipe


def _widen(engine, recipe: dict, notes: list[str]) -> dict:
    """A page opens on a default filter (the latest term, one category). Where the service accepts the same request
    with a filter left blank and then reports more pages, the blank one is kept: the request was for everything."""
    from vora.learning.recipes import Recipe

    path = recipe.get("total_pages_path")
    if not path:
        return recipe
    skip = {recipe.get("page_param"), recipe.get("size_param")}
    current = Recipe.model_validate(recipe)
    try:
        base = dig(get_json(engine, page_url(current, 0)), path)
    except (ValueError, OSError):
        return recipe
    if not isinstance(base, int):
        return recipe
    parts = urlparse(recipe["api_url"])
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        if name in skip or not value.strip() or not value.strip().isdigit():
            continue
        query = [(n, "" if n == name else v) for n, v in parse_qsl(parts.query, keep_blank_values=True)]
        trial_recipe = {**recipe, "api_url": urlunparse(parts._replace(query=urlencode(query)))}
        try:
            trial = Recipe.model_validate(trial_recipe)
            payload = get_json(engine, page_url(trial, 0))
        except (ValueError, OSError):
            continue
        pages = dig(payload, path)
        items = dig(payload, trial.records_path) if trial.records_path else payload
        if isinstance(pages, int) and pages > base and isinstance(items, list) and items:
            notes.append(f"data service: '{name}' left open ({base} -> {pages} pages)")
            recipe, base, parts = trial_recipe, pages, urlparse(trial_recipe["api_url"])
    return recipe


def _larger_pages(engine, recipe: dict, count: int, notes: list[str]) -> dict:
    """Ask for more records per request when the service allows it (fewer requests for the same data)."""
    from vora.learning.recipes import Recipe

    if not recipe.get("size_param") or not recipe.get("api_size"):
        return recipe
    for size in (100, 50):
        if size <= recipe["api_size"]:
            continue
        trial = Recipe.model_validate({**recipe, "api_size": size})
        try:
            payload = get_json(engine, page_url(trial, 0))
        except (ValueError, OSError):
            continue
        items = dig(payload, trial.records_path) if trial.records_path else payload
        if isinstance(items, list) and count < len(items) <= size:
            notes.append(f"data service: accepts {len(items)} records per request")
            return {**recipe, "api_size": size}
    return recipe
