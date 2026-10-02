"""Learn how to read a website's main listing, from its address alone.

Nothing here names a site, a selector, a column or an ID format. The learner looks at a page the way a person would:
it closes overlays, finds the biggest table of records (or list of titled links), follows a "view all / more" link when
what is on show is only a preview, works out which column is the record's own ID (and its shape), how the listing is
paged, and where each record's document lives. The result is a ``vora.learning.recipes.Recipe``, checked by running it with the
generic recipe runner before it is trusted.

Safety: it only makes reading clicks (an overlay's close control, a "view all / more" link, a pager, a record's own
download link) and refuses anything whose label suggests an account, purchase, message or deletion (the same rules as
the interactive pass, ``vora.browser.explore.is_safe_action``). It runs in a fresh browser context with no cookies and with the
network guard on (no private addresses), under a time and action budget. Everything it takes from a page is data.
"""

from __future__ import annotations

import json
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from vora.browser.explore import _FILE_LINK, _UNSAFE, _host
from vora.learning.accessibility import annotate
from vora.learning.recipes import goto, href_params, numeric_params

MAX_ACTIONS = 12
TIME_BUDGET = 150          # seconds for one learn() call
MIN_ROWS = 3               # a "table of records" has at least this many data rows
PREVIEW_ROWS = 15          # fewer rows than this and no pager: the table is probably a preview
SAMPLE_ROWS = 60
_NAV = ContextVar("structure_navigation_ms", default=30_000)
_GOAL = ContextVar("structure_goal_words", default=frozenset())   # words of the request, to prefer the listing it is about

# Words that mean "show me everything", in link or button text. Generic web vocabulary, not any site's wording.
EXPAND = re.compile(r"\b(view|see|show|browse|list)\s+(all|more|full)\b|\barchives?\b|\ball\s+(records|results|items|entries|notices|notifications|documents|publications)\b|^\s*more\s*$", re.I)
SIZE = re.compile(r"^\s*\d+(\.\d+)?\s*(b|kb|mb|gb)\s*$", re.I)
SERIAL = re.compile(r"^\s*\d+\.?\s*$")
# A model-suggested field name: one or two short lowercase words, nothing else.
FIELD_NAME = re.compile(r"^[a-z][a-z0-9]{0,15}(_[a-z0-9]{1,15})?$")

DIGEST = r"""
(limit) => {
  const visible = (el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const cssPath = (el) => {
    if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) return '#' + CSS.escape(el.id);
    const parts = [];
    for (let node = el; node && node.nodeType === 1 && node !== document.body; node = node.parentElement) {
      if (node.id && document.querySelectorAll('#' + CSS.escape(node.id)).length === 1) { parts.unshift('#' + CSS.escape(node.id)); break; }
      const same = [...node.parentElement.children].filter((c) => c.tagName === node.tagName);
      parts.unshift(node.tagName.toLowerCase() + (same.length > 1 ? `:nth-of-type(${same.indexOf(node) + 1})` : ''));
    }
    return parts.join(' > ');
  };
  // The accessible name the browser computed (see vora.learning.accessibility) is what the control is called; the text is the fallback.
  const text = (el) => (el.getAttribute('data-vora-name') || el.innerText || el.value || el.getAttribute('aria-label') ||
                        el.getAttribute('title') || el.getAttribute('alt') || '').replace(/\s+/g, ' ').trim();
  const disabled = (el) => el.disabled || el.getAttribute('aria-disabled') === 'true' || el.hasAttribute('data-vora-disabled');
  // Page chrome: regions the browser's accessibility tree calls banner, navigation, sidebar, footer or search.
  const CHROME = 'nav, header, footer, aside, [role=navigation], [role=menu], [role=menubar], [role=banner], ' +
                 '[role=complementary], [role=contentinfo], [role=search], [data-vora-ax=chrome]';
  const NEXT = /^(next|older|more results|›|»|>|>>)$/i;
  const LOAD_MORE = /\b(load|show|view|see)\s+more\b|\bmore\s+(results|items|records|entries)\b/i;
  // A grid is a <table>, or any element with grid/table roles (the markup of modern component libraries).
  const gridRows = (grid) => grid.tagName === 'TABLE'
    ? [...grid.rows].map((r) => ({el: r, cells: [...r.cells], header: !!r.querySelector('th')}))
    : [...grid.querySelectorAll('[role=row]')].map((r) => ({
        el: r, cells: [...r.querySelectorAll('[role=gridcell],[role=cell],[role=columnheader],[role=rowheader]')],
        header: !!r.querySelector('[role=columnheader]')}));
  // What the page itself attaches to a row: its id and data-* attributes (an identity a site chose, not one we guess).
  const attrsOf = (el) => { const out = {};
    for (const a of el.attributes) if ((a.name === 'id' || a.name.startsWith('data-')) && a.value && a.value.length <= 120) out[a.name] = a.value;
    return out; };
  const pagerOf = (grid) => {
    const scope = grid.parentElement || grid;
    const controls = [...grid.querySelectorAll('a')].concat([...scope.querySelectorAll('a, button, [role=button]')]);
    return controls.filter((a) => visible(a) && !disabled(a) && ((a.tagName === 'A' && /^\s*\d+\s*$/.test(a.innerText)) || a.getAttribute('rel') === 'next' ||
                                                 NEXT.test(text(a))))
      .map((a) => ({text: text(a), href: a.getAttribute('href') || '',
                    rel: a.getAttribute('rel') || '', selector: cssPath(a)})).slice(0, 20);
  };
  const moreOf = (grid) => {
    const scope = grid.parentElement || grid;
    for (const c of scope.querySelectorAll('a, button, [role=button]')) {
      if (visible(c) && !disabled(c) && LOAD_MORE.test(text(c)) && text(c).length < 40) return {selector: cssPath(c), label: text(c)};
    }
    return null;
  };
  // Tables of records: a header row and at least a few data rows with the same number of cells.
  const tables = [];
  for (const table of document.querySelectorAll('table, [role=table], [role=grid], [role=treegrid], [data-vora-grid]')) {
    // A layout wrapper holds a real table of its own; a grid's page-number row (a small nested table) does not.
    if (!visible(table) || table.closest(CHROME) || (table.tagName === 'TABLE' && [...table.querySelectorAll('table')].some((inner) => inner.rows.length >= 3))) continue;
    const rows = gridRows(table);
    const headerRow = rows.find((r) => r.header) || rows[0];
    if (!headerRow) continue;
    const width = headerRow.cells.length;
    const data = rows.filter((r) => r !== headerRow && !r.header && r.cells.length === width);
    if (data.length < 3 || width < 2) continue;
    tables.push({
      selector: cssPath(table), width, count: data.length,
      headers: headerRow.cells.map((c) => c.innerText.replace(/\s+/g, ' ').trim()),
      rows: data.slice(0, limit).map((r) => r.cells.map((c) => {
        const link = c.querySelector('a[href], input[type=image], input[type=submit], button');
        return {text: c.innerText.replace(/\s+/g, ' ').trim(),
                href: link && link.getAttribute('href') && !link.getAttribute('href').startsWith('javascript:') ? link.href : '',
                control: link ? cssPath(link) : ''};
      })),
      pager: pagerOf(table), more: moreOf(table), rowAttrs: data.slice(0, limit).map((r) => attrsOf(r.el)),
    });
  }
  // Overlays: visible fixed/absolute boxes in front of the page, with a control that dismisses them.
  const overlays = [];
  for (const el of document.querySelectorAll('body *')) {
    const s = getComputedStyle(el);
    if (!(s.position === 'fixed' || s.position === 'absolute') || !visible(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width * r.height < window.innerWidth * window.innerHeight * 0.08) continue;
    for (const c of el.querySelectorAll('a, button, input[type=button], input[type=submit], input[type=image], [role=button]')) {
      if (!visible(c)) continue;
      const label = text(c);
      const looks = /close|cancel|dismiss|cross/i.test((c.className || '') + ' ' + (c.id || '') + ' ' +
                     (c.getAttribute('src') || '') + ' ' + ((c.querySelector('img') || {}).src || ''));
      if (looks || /^(x|×|✕|close|ok|okay|cancel|dismiss|got it|no thanks|skip)$/i.test(label)) {
        overlays.push({selector: cssPath(c), label}); break;
      }
    }
    if (overlays.length >= 5) break;
  }
  // Controls that might show the full listing.
  const expand = [...document.querySelectorAll('a, button, input[type=button], input[type=submit], [role=button], [role=link], [data-vora-name]')]
    .filter((el) => visible(el) && !disabled(el)).map((el) => ({selector: cssPath(el), label: text(el), href: el.getAttribute('href') || '',
                                   inForm: !!el.closest('form'), type: (el.getAttribute('type') || '')}))
    .filter((x) => x.label && x.label.length < 60);
  // Collections of links: any element whose children repeat one structure (same tag and classes) and each hold a
  // link: a feed of cards, a list, an index of documents. Navigation (nav/header/footer, menus) is skipped.
  const collections = [];
  const signature = (el) => el.tagName + '.' + [...el.classList].sort().join('.');
  const largest = (groups) => Object.values(groups).sort((a, b) => b.length - a.length)[0] || [];
  // Items of one kind: children with the same tag and classes; when the classes differ (generated names), children of
  // the same tag with the same size (a grid of cards) or the same left edge and width (stacked rows): layout geometry.
  const repeatedKids = (box) => {
    const kids = [...box.children].filter(visible);
    const bySignature = {}, bySize = {}, byColumn = {};
    for (const kid of kids) {
      const r = kid.getBoundingClientRect(); const tag = kid.tagName;
      (bySignature[signature(kid)] = bySignature[signature(kid)] || []).push(kid);
      const size = tag + '|' + Math.round(r.width / 8) + '|' + Math.round(r.height / 24);
      (bySize[size] = bySize[size] || []).push(kid);
      const column = tag + '|' + Math.round(r.left / 6) + '|' + Math.round(r.width / 8);
      (byColumn[column] = byColumn[column] || []).push(kid);
    }
    const best = largest(bySignature);
    if (best.length >= 3) return best;
    const geometric = [largest(bySize), largest(byColumn)].sort((a, b) => b.length - a.length)[0];
    return geometric.length > best.length ? geometric : best;
  };
  let scanned = 0;
  for (const box of document.querySelectorAll('table, ul, ol, div, section, main, article, [role=list], [role=feed]')) {
    if (++scanned > 4000) break;
    if (box.closest(CHROME) || !visible(box)) continue;
    let els, itemSelector;
    if (box.tagName === 'TABLE') {
      els = [...box.querySelectorAll(':scope > tbody > tr, :scope > tr')]; itemSelector = 'tr';
    } else {
      els = repeatedKids(box);
      if (els.length) {
        // the classes every item shares (generated class names differ per item: then the element type alone)
        const shared = [...els[0].classList].filter((c) => els.every((e) => e.classList.contains(c)));
        itemSelector = els[0].tagName.toLowerCase() + shared.map((c) => '.' + CSS.escape(c)).join('');
      }
    }
    if (els.length < 3) continue;
    const items = []; let more = null;
    for (const el of els) {
      const anchors = [...el.querySelectorAll('a[href]')].filter((a) => {
        const h = a.getAttribute('href'); return h && !h.startsWith('javascript:') && !h.startsWith('#');
      });
      if (!anchors.length || anchors.length > 8) continue;
      const label = (a) => (a.innerText || a.getAttribute('aria-label') || a.title || '').replace(/\s+/g, ' ').trim();
      const a = anchors.reduce((x, y) => (label(y).length > label(x).length ? y : x));
      const title = label(a);
      if (title.length < 40 && /\bmore\b|\bview all\b|\bsee all\b|^\s*all\b/i.test(title)) { more = {selector: cssPath(a), label: title}; continue; }
      items.push({title, href: a.href, text: el.innerText.replace(/\s+/g, ' ').trim().slice(0, 400)});
    }
    if (items.length < 3 || new Set(items.map((i) => i.href)).size < items.length * 0.8) continue;
    const titled = items.filter((i) => i.title.length >= 6).length;
    if (titled < items.length * 0.6) continue;
    collections.push({selector: cssPath(box), itemTag: itemSelector, count: items.length, items: items.slice(0, limit), more});
  }
  return {url: location.href, title: document.title, tables, overlays, controls: expand, collections};
}
"""


@dataclass
class TableInfo:
    selector: str
    headers: list[str]
    rows: list[list[dict]]
    count: int
    pager: list[dict]
    more: dict | None = None          # a "load more" control that makes the same table longer
    row_attrs: list[dict] = field(default_factory=list)       # id and data-* attributes of each row element

    def column(self, index: int) -> list[str]:
        return [row[index]["text"] for row in self.rows if index < len(row)]


@dataclass
class CollectionInfo:
    selector: str
    item_tag: str
    items: list[dict]
    count: int
    more: dict | None

    @property
    def score(self) -> float:
        return self.count * (sum(len(i["title"]) for i in self.items) / max(1, len(self.items))) / 10


@dataclass
class LearnedStructure:
    ok: bool
    url: str
    recipe: dict | None = None
    steps: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    sample: list[dict] = field(default_factory=list)
    alternatives: list[dict] = field(default_factory=list)     # other recipes for the same listing (other identities)
    # Other sections of the site learned from links the request points at: [{"url", "recipe", "sample"}].
    sections: list[dict] = field(default_factory=list)


def is_serial(values: list[str]) -> bool:
    numbers = [int(re.sub(r"\D", "", v)) for v in values if SERIAL.match(v)]
    return len(numbers) == len(values) and numbers == list(range(numbers[0], numbers[0] + len(numbers))) if numbers else False


def is_size(values: list[str]) -> bool:
    return bool(values) and all(SIZE.match(v) for v in values if v)


def shape_tokens(value: str) -> list[tuple[str, str]]:
    """'AB-12-x7' -> [('A','AB'),('S','-'),('D','12'),('S','-'),('A','x'),('D','7')]: letters, digits, separators."""
    return [("A" if m.group(0)[0].isalpha() else "D" if m.group(0)[0].isdigit() else "S", m.group(0))
            for m in re.finditer(r"[A-Za-z]+|\d+|[^A-Za-z\d]+", value)]


def id_regex(values: list[str]) -> str | None:
    """A regex for an identifier column, generalised from the values' shared shape, or None if they share none."""
    shapes = [shape_tokens(v) for v in values]
    kinds = [tuple(kind for kind, _ in s) for s in shapes]
    if len(set(kinds)) != 1 or "D" not in kinds[0]:
        return None
    parts = []
    for position, kind in enumerate(kinds[0]):
        texts = [s[position][1] for s in shapes]
        if kind == "S":
            if len(set(texts)) != 1:
                return None
            parts.append(re.escape(texts[0]))
            continue
        lengths = {len(t) for t in texts}
        counter = len(set(texts)) == len(texts)       # every row different: a running number, its length may grow
        if kind == "D":
            parts.append(rf"\d{{{lengths.pop()}}}" if len(lengths) == 1 and not counter else r"\d+")
        else:
            letters = "[A-Z]" if all(t.isupper() for t in texts) else "[a-z]" if all(t.islower() for t in texts) else "[A-Za-z]"
            parts.append(f"{letters}{{{lengths.pop()}}}" if len(lengths) == 1 else f"{letters}+")
    return "^" + "".join(parts) + "$"


def unique_values(values: list[str]) -> bool:
    """Every value present and different from every other."""
    return len(values) >= MIN_ROWS and all(values) and len(set(values)) == len(values)


def row_params(table: TableInfo) -> list[dict[str, str]]:
    """The parameters of the links in each row (and the last path segment as "path")."""
    found = []
    for row in table.rows:
        params: dict[str, str] = {}
        for cell in row:
            if cell["href"]:
                for key, value in href_params(cell["href"]).items():
                    params.setdefault(key, value)
        found.append(params)
    return found


def id_candidates(table: TableInfo, keep: list[int]) -> list[dict]:
    """What can identify a record, best evidence first:

    1. an attribute the site put on the row (``id``, ``data-*``);
    2. a parameter of a link in the row (``?doc=...``, ``/item/<key>``);
    3. a column whose values are all present and different, the most key-like first (no spaces, similar lengths:
       a compact code over a title), then the leftmost.

    Each must be unique within the page; whether it stays unique across pages and reloads is decided later by running
    the recipe (see ``learn_page``), not by how the values look."""
    found = []
    if len(table.row_attrs) == len(table.rows):
        for name in sorted({key for attrs in table.row_attrs for key in attrs}):
            values = [attrs.get(name, "") for attrs in table.row_attrs]
            if unique_values(values):
                found.append({"source": "attr", "name": name, "values": values})
    params = row_params(table)
    for name in sorted({key for row in params for key in row}):
        values = [row.get(name, "") for row in params]
        if unique_values(values):
            found.append({"source": "param", "name": name, "values": values})
    columns = []
    for index in keep:
        values = table.column(index)
        if unique_values(values) and not is_serial(values) and not is_size(values):
            lengths = [len(v) for v in values]
            spread = (max(lengths) - min(lengths)) / max(1, sum(lengths) / len(lengths))
            columns.append(((any(" " in v for v in values), round(spread, 1), index),
                            {"source": "column", "index": index, "values": values}))
    return found + [candidate for _, candidate in sorted(columns, key=lambda item: item[0])]


def find_id_column(table: TableInfo, keep: list[int]) -> tuple[int, str] | None:
    """The column that would identify a record (see ``id_candidates``), with the pattern its values share (or any)."""
    for candidate in id_candidates(table, keep):
        if candidate["source"] == "column":
            return candidate["index"], (id_regex(candidate["values"]) or ".+")
    return None


def slug(header: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", header.lower())).strip("_") or "column"


def pager_kind(table: TableInfo) -> dict:
    """How the table is paged, read off its numbered links."""
    for link in table.pager:
        if link["rel"] == "next" or re.fullmatch(r"(?i)next|older|more results|›|»|>|>>", link["text"].strip()):
            return {"type": "next_link", "max_pages": 5}
        number = link["text"].strip()
        call = re.search(r"\(\s*'[^']*'\s*,\s*'([^']*)'\s*\)", link["href"])   # javascript postback: (target, argument)
        if call and number and call.group(1).endswith(number):
            return {"type": "postback", "pattern": call.group(1)[: -len(number)], "max_pages": 5}
        if re.search(rf"[?&][a-z_]*=\s*{number}\b", link["href"], re.I):
            return {"type": "next_link", "max_pages": 5}
    return {"type": "none", "max_pages": 1}


def score_table(table: TableInfo) -> float:
    filled = [i for i in range(len(table.headers)) if any(table.column(i))]
    typed = sum(1 for i in filled if any(re.search(r"\d", v) for v in table.column(i)))
    link_only = all(row[0]["href"] for row in table.rows) and len(filled) <= 1
    words = _GOAL.get()
    text = " ".join([*table.headers, *(table.column(i)[0] for i in filled if table.column(i))]).lower()
    about = sum(1 for word in words if word in text)             # headers or first row share words with the request
    return table.count * len(filled) + 15 * typed + (40 if table.pager else 0) - (200 if link_only else 0) + 60 * about


def has_headers(table: TableInfo) -> bool:
    """A real header row names the columns; a first row full of values (dates, IDs, long text) is data, not a header."""
    typed = sum(1 for h in table.headers if re.search(r"\d{2,}", h) or len(h) > 70)
    return typed * 2 < max(1, len([h for h in table.headers if h]))


def collections_of(data: dict) -> list[CollectionInfo]:
    found = [CollectionInfo(c["selector"], c["itemTag"], c["items"], c["count"], c["more"]) for c in data.get("collections", [])]
    return sorted(found, key=lambda c: c.score, reverse=True)


def grew(new, old) -> bool:
    """Is ``new`` (reached by following a "view all / more" link) a better listing than ``old``?"""
    if new is None:
        return False
    if old is None:
        return True
    if new[0] == "table":
        if old[0] == "list":
            return True
        return new[1].count > old[1].count or (bool(new[1].pager) and not old[1].pager)
    if old[0] == "table":
        return False
    # a list reached through its own "more" link: the fuller listing may only be a little longer
    return new[1].count >= old[1].count - 2


def is_preview(structure) -> bool:
    if structure is None:
        return True
    kind, found = structure
    if kind == "table":
        return found.count < PREVIEW_ROWS and not found.pager
    return found.more is not None or found.count < PREVIEW_ROWS


def template_from(observed: list[tuple[str, str]], pattern: str | None) -> dict | None:
    """Find the parts of the ID that appear in the document address and turn them into a link rule."""
    first_id, first_url = observed[0]
    if len(first_id) >= 4 and all(identifier in url for identifier, url in observed):
        whole = {"from": "id", "pattern": "^(.+)$", "template": first_url.replace(first_id, "{1}", 1)}
        if whole["template"].startswith("https://") and all(
                whole["template"].replace("{1}", identifier) == url for identifier, url in observed):
            return whole
    if not pattern:
        return None
    tokens = shape_tokens(first_id)
    pieces = []   # (token position, start, end) of digit slices found in the URL, longest first
    for position, (kind, value) in enumerate(tokens):
        if kind != "D":
            continue
        best = None
        for start in range(len(value)):
            for end in range(len(value), start + 3, -1):
                piece = value[start:end]
                if re.search(rf"(?<!\d){piece}(?!\d)", first_url) and (not best or end - start > best[2] - best[1]):
                    best = (position, start, end)
        if best:
            pieces.append(best)
    if not pieces:
        return None
    # Rebuild the ID regex with capture groups around the found slices.
    regex_parts = re.findall(r"\\d\{\d+\}|\\d\+|\[[A-Za-z\-]+\]\{\d+\}|\[[A-Za-z\-]+\]\+|\\.|.", pattern.strip("^$"))
    token_regex = []
    for kind, value in tokens:
        token_regex.append(re.escape(value) if kind == "S" else None)
    # Walk the pattern alongside the tokens: one regex part per token (separators are escaped literals).
    out, part_index = [], 0
    groups_for_url = []
    for position, (kind, value) in enumerate(tokens):
        if kind == "S":
            literal = re.escape(value)
            out.append(literal)
            part_index += len(re.findall(r"\\.|.", literal))
            continue
        part = regex_parts[part_index]
        part_index += 1
        slice_ = next((p for p in pieces if p[0] == position), None)
        if not slice_:
            out.append(part)
            continue
        _, start, end = slice_
        before, inside, after = start, end - start, len(value) - end
        variable = part.endswith("+")
        grouped = (rf"\d{{{before}}}" if before else "") + (r"(\d+)" if variable and not after else rf"(\d{{{inside}}})")
        grouped += rf"\d{{{after}}}" if after else ""
        out.append(grouped)
        groups_for_url.append(value[start:end])
    regex = "^" + "".join(out) + "$"
    template = first_url
    for number, text in enumerate(groups_for_url, 1):
        template = re.sub(rf"(?<!\d){text}(?!\d)", "{" + str(number) + "}", template, count=1)
    rule = {"from": "id", "pattern": regex, "template": template}
    # The rule must reproduce every observed address.
    for identifier, url in observed:
        match = re.search(regex, identifier)
        if not match:
            return None
        built = template
        for number, group in enumerate(match.groups(), 1):
            built = built.replace("{" + str(number) + "}", group)
        if built != url:
            return None
    return rule if template.startswith("https://") else None


def find_item_id(items: list[dict]) -> dict | None:
    """Items are identified by a number in their address when they have one (the parameter's name varies between
    kinds of items, so any numeric part counts and (name, number) pairs must not repeat); otherwise by the address
    itself (a slug, a hash, a text key), when the addresses are all different."""
    pairs, params = [], set()
    for item in items:
        numbers = numeric_params(item["href"])
        if numbers:
            key = sorted(numbers)[0] if "path" not in numbers or len(numbers) == 1 else sorted(k for k in numbers if k != "path")[0]
            pairs.append((key, numbers[key]))
            params.update(numbers)
    if len(pairs) >= 0.8 * len(items) and len(set(pairs)) == len(pairs):
        return {"params": sorted(params), "from": "address"}
    addresses = {item["href"].split("#")[0] for item in items}
    if len(addresses) >= 0.95 * len(items) and len(addresses) > 1:
        return {"params": [], "from": "address"}
    return None


# --------------------------------------------------------------------------------------------- browser work


def nav_ms() -> int:
    return _NAV.get()


def digest(page, wait: float = 0) -> tuple[dict, list["TableInfo"]]:
    """Summarise the page. With ``wait``, keep looking for up to that many seconds until a table of records shows up
    (a page that has just changed may not have drawn its table yet)."""
    deadline = time.monotonic() + wait
    annotate(page)                       # the browser's account of regions, grids and control names (see vora.learning.accessibility)
    while True:
        try:
            data = page.evaluate(DIGEST, SAMPLE_ROWS)
        except Exception:  # noqa: BLE001 - the page was still navigating
            data = {"tables": [], "overlays": [], "controls": [], "collections": [], "url": page.url, "title": ""}
        tables = [TableInfo(t["selector"], t["headers"], t["rows"], t["count"], t["pager"], t.get("more"),
                           t.get("rowAttrs") or []) for t in data["tables"]]
        tables = [t for t in tables if has_headers(t)]
        if tables or time.monotonic() >= deadline:
            return data, sorted(tables, key=score_table, reverse=True)
        time.sleep(0.7)


def snapshot(page, wait: float = 0):
    """The best structure on the page: a table of records if there is one, else a collection of links."""
    data, tables = digest(page, wait=wait)
    collections = collections_of(data)
    if tables:
        return data, ("table", tables[0])
    if collections:
        return data, ("list", collections[0])
    return data, None


def safe_to_click(control: dict, page_url: str) -> bool:
    """A control found while looking for the listing may be clicked only if it just reveals content."""
    if str(control.get("type", "")).lower() in {"submit", "reset", "file"}:
        return False
    label = " ".join(str(control.get("label", "")).split())
    if not label or len(label) > 80 or _UNSAFE.search(label):
        return False
    href = (control.get("href") or "").strip()
    if href and not href.startswith(("#", "javascript:")):
        target = urlparse(urljoin(page_url, href))
        if target.scheme not in {"http", "https"} or _FILE_LINK.search(target.path):
            return False
        if _host(target.netloc) != _host(urlparse(page_url).netloc):
            return False
    return True


def click(page, selector: str | None = None, label: str | None = None) -> bool:
    """Click a control; wait for a navigation if it causes one. True if the click happened."""
    try:
        if selector:
            target = page.locator(selector).first
        else:
            safe = (label or "").replace('"', '\\"')
            target = page.locator(f'a:text-is("{safe}"), button:text-is("{safe}"), input[value="{safe}"]').first
        if target.count() == 0:
            return False
        try:
            with page.expect_navigation(timeout=8000):
                target.click(timeout=5000)
        except Exception:  # noqa: BLE001 - clicks that change the page in place
            pass
        page.wait_for_load_state("domcontentloaded", timeout=nav_ms())
        time.sleep(1)
        return True
    except Exception:  # noqa: BLE001
        return False


def close_overlays(page, steps: list[dict], log: list[str]) -> None:
    for _ in range(3):
        data, _ = digest(page)
        if not data["overlays"]:
            return
        overlay = data["overlays"][0]
        if _UNSAFE.search(overlay.get("label", "")):
            return
        if click(page, overlay["selector"]):
            steps.append({"click": overlay["selector"], "optional": True})
            log.append(f"closed an overlay ({overlay['label'] or overlay['selector']})")
        else:
            return


def replay(page, steps: list[dict]) -> None:
    for step in steps:
        click(page, step.get("click"), step.get("click_text"))


def reach_listing(page, url: str, log: list[str]):
    """Open the page, close overlays, and follow "view all / more" links while that gives a better listing."""
    steps: list[dict] = []
    goto(page, url, nav_ms())
    time.sleep(2)
    close_overlays(page, steps, log)
    data, best = snapshot(page, wait=4)
    tried: set[str] = set()
    actions = 0
    while actions < MAX_ACTIONS and is_preview(best):
        candidates = [c for c in data["controls"] if EXPAND.search(c["label"]) and c["selector"] not in tried]
        if best and best[0] == "list" and best[1].more:
            candidates.insert(0, {**best[1].more, "href": "", "inForm": False, "type": ""})
        candidates = [c for c in candidates if c["selector"] not in tried and safe_to_click(c, page.url)]
        if not candidates:
            break
        control = candidates[0]
        tried.add(control["selector"])
        actions += 1
        before = page.url
        if not click(page, selector=control["selector"]):
            continue
        close_overlays(page, [], log)
        _, after = snapshot(page, wait=8)
        if grew(after, best):
            steps.append({"click": control["selector"], "optional": False, "navigates": page.url != before})
            kind, found = after
            log.append(f"followed '{control['label']}': {found.count} {'rows' if kind == 'table' else 'items'}"
                       + (" with a pager" if kind == "table" and found.pager else ""))
            best = after
            data, _ = digest(page)
        else:
            log.append(f"'{control['label']}' did not reveal a better listing; going back")
            goto(page, url, nav_ms())
            time.sleep(1)
            replay(page, steps)
            close_overlays(page, [], log)
    return steps, best


def document_links(page, url: str, steps: list[dict], table: "TableInfo", id_values: list[str], id_index: int,
                   log: list[str]) -> tuple[str | None, dict | None]:
    """Where each record's document is. Direct links are kept as a column; a download control without a link is
    clicked for two rows, and the address it leads to is turned into a template built from the row's ID."""
    for index in range(len(table.headers)):
        hrefs = [row[index]["href"] for row in table.rows if index < len(row)]
        if hrefs and all(hrefs) and index != id_index:
            return None, None  # the table carries its own links; they become <field>_url columns
    controls = [i for i in range(len(table.headers))
                if all(row[i]["control"] and not row[i]["href"] for row in table.rows[:2])]
    if not controls:
        return None, None
    observed = []
    for row_number in (0, 1):
        found = capture_document(page, table, row_number, controls[-1])
        if found:
            observed.append((id_values[row_number], found))
        goto(page, url, nav_ms())
        time.sleep(1)
        replay(page, steps)
    if len(observed) < 2:
        log.append("could not see where the download links lead")
        return None, None
    rule = template_from(observed, id_regex(id_values))
    if rule:
        log.append(f"document link rebuilt from the ID: {rule['template']}")
    else:
        log.append(f"download addresses do not follow the ID: {[u for _, u in observed]}")
    if not rule:
        return None, None
    kind = re.search(r"\.([A-Za-z0-9]{2,5})(?:\?|$)", rule["template"])   # the file type the address ends in
    return (f"{kind.group(1).lower()}_url" if kind else "document_url"), rule


def capture_document(page, table: "TableInfo", row_number: int, column: int) -> str | None:
    """Click one row's download control and return the document address it leads to."""
    seen: list[str] = []
    files: list[str] = []
    context = page.context
    listener = lambda request: seen.append(request.url)  # noqa: E731

    def on_response(response) -> None:
        # A response that is a file: sent as an attachment, or of a type a browser shows as a document, not a page.
        headers = response.headers
        kind = headers.get("content-type", "").split(";")[0].strip().lower()
        if "attachment" in headers.get("content-disposition", "").lower() or (
                kind.startswith(("application/", "audio/", "video/")) and not kind.endswith(("json", "javascript", "xml", "xhtml+xml"))):
            files.append(response.url)

    context.on("request", listener)
    context.on("response", on_response)
    try:
        selector = table.rows[row_number][column]["control"]
        try:
            with page.expect_download(timeout=10_000) as download:
                page.locator(selector).first.click(timeout=5000)
            seen.append(download.value.url)
        except Exception:  # noqa: BLE001 - not a download: a navigation or a new tab
            time.sleep(4)
        for open_page in context.pages:
            seen.append(open_page.url)
            try:
                for source in open_page.evaluate(
                        "() => [...document.querySelectorAll('embed,object,iframe')]"
                        ".map(e => e.src || e.data || e.getAttribute('original-url') || '')"):
                    seen.append(source)
            except Exception:  # noqa: BLE001
                pass
        for extra in context.pages[1:]:
            extra.close()
    finally:
        context.remove_listener("request", listener)
        context.remove_listener("response", on_response)
    documents = files or [u for u in seen if re.search(r"\.(pdf|docx?|xlsx?|csv|zip|pptx?|odt|ods)(\?|$)", u, re.I)]
    return documents[0] if documents else None


def table_recipe(table: "TableInfo", steps: list[dict], columns: dict[str, str], candidate: dict) -> dict:
    """The recipe for reading ``table`` when its records are identified by ``candidate``."""
    recipe = {"kind": "table", "ready_steps": steps, "table": table.selector, "columns": columns,
              "pagination": pager_kind(table), "links": {}, "id_source": "column"}
    if candidate["source"] == "column":
        recipe["id_field"] = columns[table.headers[candidate["index"]]]
        # The shape the values share is kept only as a drift check (a layout change breaks it); it never chose them.
        recipe["id_pattern"] = id_regex(candidate["values"]) or ".+"
    else:
        field_name = "row_id"
        while field_name in columns.values():
            field_name += "_"
        recipe.update(id_field=field_name, id_pattern=".+", id_source=candidate["source"], id_name=candidate["name"])
    return recipe


def grows_by_itself(page, table: "TableInfo", log: list[str]) -> dict | None:
    """A listing that gets longer without a pager: a "load more" control, or scrolling to the end. Tried, not assumed:
    the pagination is returned only when the same table really has more rows afterwards."""
    before = table.count
    if table.more:
        control = {**table.more, "href": "", "type": ""}
        if safe_to_click(control, page.url) and click(page, selector=table.more["selector"]):
            _, found = snapshot(page, wait=3)
            if found and found[0] == "table" and found[1].selector == table.selector and found[1].count > before:
                log.append(f"the listing grows with '{table.more['label']}'")
                return {"type": "load_more", "control": table.more["selector"], "max_pages": 10}
        return None
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    except Exception:  # noqa: BLE001
        return None
    time.sleep(2.5)
    _, found = snapshot(page, wait=2)
    if found and found[0] == "table" and found[1].selector == table.selector and found[1].count > before:
        log.append("the listing grows as the page is scrolled")
        return {"type": "scroll", "max_pages": 10}
    return None


def clean_names(names, headers: list[str]) -> list[str] | None:
    """Field names suggested by a model, accepted only when they are exactly what was asked for."""
    if not isinstance(names, list) or len(names) != len(headers):
        return None
    cleaned = [str(n).strip() for n in names]            # taken as given: a name with spaces or capitals is not a field name
    if len(set(cleaned)) != len(cleaned) or not all(FIELD_NAME.match(n) for n in cleaned):
        return None
    return cleaned


# --------------------------------------------------------------------------------------------- learn


def learn_list(steps: list[dict], found: "CollectionInfo", log: list[str]) -> dict | None:
    rule = find_item_id(found.items)
    if not rule:
        log.append("the items' addresses do not tell them apart")
        return None
    log.append("records are links; each is identified by "
               + (f"a number in its address ({', '.join(rule['params'])})" if rule["params"] else "its address"))
    return {"kind": "link_list", "ready_steps": steps, "container": found.selector, "item": found.item_tag,
            "id_field": "id", "id_params": rule["params"], "pagination": {"type": "none", "max_pages": 1}}


def learn(engine, url: str, *, names_fn=None, verify: bool = True, budget: float = TIME_BUDGET,
          goal: str = "", hops: int = 1) -> "LearnedStructure":
    """Learn how to read the listing at ``url``: first from the page itself (a table or a list of links); if the page
    gives nothing readable, from the data service it loads the listing from (see ``vora.learning.api_records``); if the page
    is only a way in (a home page), from the pages its links lead to that the request ``goal`` is about."""
    started = time.monotonic()
    from vora.extraction.semantics import topic_tokens

    token = _GOAL.set(frozenset(word for word in topic_tokens(goal) if len(word) >= 4) if goal else frozenset())
    try:
        result = learn_here(engine, url, names_fn=names_fn, verify=verify, budget=budget)
    finally:
        _GOAL.reset(token)
    if result.ok:
        result = prefer_service(engine, url, result)
    if result.ok or not goal or hops <= 0:
        return result
    for link in section_links(engine, url, goal):
        left = budget - (time.monotonic() - started)
        if left < 45 or len(result.sections) >= MAX_SECTIONS:
            break
        sub = learn(engine, link, names_fn=names_fn, verify=verify, budget=min(left, 90), goal=goal, hops=hops - 1)
        result.notes.append(f"followed '{link}': " + ("learned" if sub.ok else "nothing readable"))
        for section in sub.sections or ([{"url": link, "recipe": sub.recipe, "sample": sub.sample}] if sub.ok else []):
            result.sections.append(section)
    if result.sections:
        first = result.sections[0]
        result.ok, result.recipe, result.sample = True, first["recipe"], first["sample"]
    return result


MAX_SECTIONS = 4
_SKIP_LINK = re.compile(r"login|logout|sign.?in|register|contact|feedback|privacy|terms|cookie|disclaimer|sitemap|"
                        r"javascript:|mailto:|tel:|\.(?:pdf|docx?|xlsx?|zip|jpe?g|png|gif|mp4)(?:$|\?)", re.I)


def section_links(engine, url: str, goal: str) -> list[str]:
    """Same-site pages linked from ``url`` whose address or label shares words with the request, best first."""
    from urllib.parse import urljoin, urlparse

    from vora.extraction.semantics import topic_tokens

    from vora.browser.engine import new_context
    from vora.learning.recipes import goto

    wanted = {token for token in topic_tokens(goal) if len(token) >= 4}
    if not wanted:
        return []
    context = new_context(engine)
    try:
        page = context.new_page()
        page.set_default_timeout(engine.settings.navigation_timeout_ms)
        menus: list[str] = []

        def on_response(response) -> None:
            # Pages whose menus are not anchors often load them as data: same-site paths in a JSON answer.
            try:
                if response.request.resource_type in {"xhr", "fetch"} and "json" in response.headers.get("content-type", ""):
                    menus.extend(re.findall(r'"(/[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-]+){0,4})"', response.text()[:400_000]))
            except Exception:  # noqa: BLE001
                pass

        page.on("response", on_response)
        try:
            goto(page, url, engine.settings.navigation_timeout_ms)
            page.wait_for_timeout(4000)
            anchors = page.eval_on_selector_all(
                "a[href]", "els => els.map(a => [(a.innerText || a.title || '').replace(/\\s+/g, ' ').trim().slice(0, 80), a.href])")
            here = page.url
            anchors = [*anchors, *[["", path] for path in dict.fromkeys(menus)]]
        except Exception:  # noqa: BLE001
            return []
    finally:
        context.close()
    site = (urlparse(here).hostname or "").removeprefix("www.")
    scored: dict[str, tuple[int, int]] = {}
    for label, href in anchors:
        address = urljoin(here, href).split("#")[0]
        parts = urlparse(address)
        if (parts.hostname or "").removeprefix("www.") != site or _SKIP_LINK.search(address) or address.rstrip("/") == here.rstrip("/"):
            continue
        words = set(re.findall(r"[a-z]{4,}", f"{label} {parts.path}".lower()))
        hits = len({w for w in wanted if any(word.startswith(w[:5]) or w.startswith(word[:5]) for word in words)})
        if hits:
            scored.setdefault(address, (-hits, len(parts.path)))
    return sorted(scored, key=lambda address: scored[address])[:MAX_SECTIONS + 2]


def prefer_service(engine, url: str, result: "LearnedStructure") -> "LearnedStructure":
    """A listing read from the page is only what the page drew. When the page drew it from a paged data service that
    gives at least as much, that service is the complete, layout-independent source: it replaces the page reading."""
    recipe = result.recipe or {}
    if recipe.get("kind") == "json_api" or (recipe.get("pagination") or {}).get("type") != "none" or result.sections:
        return result
    from vora.learning.api_records import learn_api

    notes: list[str] = []
    try:
        service = learn_api(engine, url, notes)
    except Exception:  # noqa: BLE001 - the page reading stands
        return result
    if not service or not (service.get("page_param") or service.get("next_path")):
        return result
    verified, sample, why = check(engine, url, service)
    if verified and len(sample) >= len(result.sample):
        result.notes.extend([*notes, why, "the page's listing comes from a paged data service: read from the service"])
        result.recipe, result.sample = service, sample
    return result


def learn_here(engine, url: str, *, names_fn=None, verify: bool = True, budget: float = TIME_BUDGET) -> "LearnedStructure":
    """The two ways of learning one page: from the page itself, else from its data service."""
    result = learn_page(engine, url, names_fn=names_fn, verify=verify, budget=budget)
    if result.ok:
        return result
    from vora.learning.api_records import learn_api

    notes: list[str] = []
    try:
        recipe = learn_api(engine, url, notes)
    except Exception as exc:  # noqa: BLE001 - this is a second try; the first result stands
        notes.append(f"data service: failed ({type(exc).__name__})")
        recipe = None
    result.notes.extend(notes)
    if recipe:
        verified, sample, why = check(engine, url, recipe)
        result.notes.append(why)
        if verified:
            result.recipe, result.sample, result.ok = recipe, sample, True
    return result


def learn_page(engine, url: str, *, names_fn=None, verify: bool = True, budget: float = TIME_BUDGET) -> "LearnedStructure":
    """Learn the listing structure of ``url`` with the engine's browser. ``names_fn(headers, sample_rows)`` may
    suggest short field names (a model call); without it, or if its answer is not clean, plain header slugs are used."""
    from vora.browser.engine import new_context

    result = LearnedStructure(ok=False, url=url)
    started = time.monotonic()
    token = _NAV.set(engine.settings.navigation_timeout_ms)
    context = new_context(engine, downloads=True)
    try:
        page = context.new_page()
        page.set_default_timeout(nav_ms())
        try:
            steps, found = reach_listing(page, url, result.notes)
        except Exception as exc:  # noqa: BLE001 - the site did not load, or changed under us
            result.notes.append(f"could not open the page: {type(exc).__name__}")
            return result
        if found is None:
            result.notes.append("no table of records and no list of titled links found")
            return result
        result.steps = [json.dumps(step) for step in steps]
        kind, structure = found
        if kind == "list":
            result.recipe = learn_list(steps, structure, result.notes)
            if result.recipe is None:
                return result
        else:
            table = structure
            keep = [i for i in range(len(table.headers)) if any(table.column(i)) and not is_serial(table.column(i))]
            candidates = id_candidates(table, keep)[:3]
            if not candidates:
                result.notes.append("nothing in the table tells its records apart")
                return result
            headers = [table.headers[i] for i in keep]
            samples = [[row[i]["text"][:80] for i in keep] for row in table.rows[:3]]
            try:
                names = clean_names(names_fn(headers, samples), headers) if names_fn else None
            except Exception:  # noqa: BLE001 - naming is cosmetic
                names = None
            names = names or [slug(h) for h in headers]
            columns = dict(zip(headers, names))
            recipes = [table_recipe(table, steps, columns, candidate) for candidate in candidates]
            primary = recipes[0]
            note = candidates[0]
            result.notes.append(f"records identified by {note['source']} "
                                f"{note.get('name') or table.headers[note['index']]!r}")
            if time.monotonic() - started < budget:
                id_index = note.get("index", -1)
                link_field, rule = document_links(page, url, steps, table, note["values"], id_index, result.notes)
                if rule:
                    rule["from"] = primary["id_field"]
                    primary["links"][link_field] = rule
            if primary["pagination"]["type"] == "none" and time.monotonic() - started < budget:
                longer = grows_by_itself(page, table, result.notes)
                if longer:
                    for recipe in recipes:
                        recipe["pagination"] = longer
            result.recipe, result.alternatives = primary, recipes[1:]
    finally:
        context.close()
        _NAV.reset(token)
    if verify:
        # The first identity that holds up when the recipe is actually run (unique across pages) is the one kept;
        # an attribute that only numbers the rows on each page fails here and the next candidate is tried.
        for recipe in [result.recipe, *result.alternatives]:
            verified, sample, why = check(engine, url, recipe)
            result.notes.append(why)
            if verified:
                result.recipe, result.sample, result.ok = recipe, sample, True
                break
            result.sample = sample
    else:
        result.ok = True
    result.alternatives = []
    return result


def _plain_get(url: str, *, timeout: float = 10, max_bytes: int = 300_000, headers: dict | None = None):
    import httpx

    response = httpx.get(url, timeout=timeout, follow_redirects=True, verify=False, headers=headers)
    return str(response.url), response.status_code, response.text[:max_bytes]


def check(engine, url: str, recipe: dict, pages: int = 2) -> tuple[bool, list[dict], str]:
    """Run the learned recipe with the generic runner: it must give rows with valid, unique IDs, reach page 2 when
    the listing is paged, and its document links must answer."""
    from vora.shared.urls import safe_get

    from vora.learning.recipes import Recipe, run_recipe

    fetch = safe_get if engine.settings.guard_network else _plain_get      # no guard only in tests with local servers
    try:
        parsed = Recipe.model_validate(recipe)
    except Exception as exc:  # noqa: BLE001
        return False, [], f"the learned recipe is not valid ({type(exc).__name__})"
    outcome = run_recipe(engine, url, parsed, max_pages=pages)
    rows = [row.fields for row in outcome.rows]
    if not outcome.ok or not rows:
        return False, [], f"recipe did not run: {outcome.note}"
    ids = [row.get(parsed.id_field) for row in rows]
    # a page that is not full is the whole listing
    if (parsed.kind == "json_api" and outcome.pages < 2 and parsed.pagination.max_pages > 1
            and len(rows) >= (parsed.api_size or 1)):
        return False, rows, "the data service did not give a second page"
    if len(set(ids)) != len(ids):
        return False, rows, "duplicate IDs across pages"
    if parsed.kind == "table":
        if parsed.pagination.type != "none" and outcome.pages < 2:
            return False, rows, "the pager did not reach page 2"
        link_names = list(parsed.links)
    else:
        link_names = ["url"]
    for row in [r for r in rows if any(name in r for name in link_names)][:2]:
        for name in link_names:
            try:
                _, status, _ = fetch(row[name], timeout=20, max_bytes=2048)
            except (ValueError, OSError) as exc:
                return False, rows, f"{name} for {row.get(parsed.id_field)} could not be fetched ({type(exc).__name__})"
            if status >= 400:
                return False, rows, f"{name} for {row.get(parsed.id_field)} answered {status}"
    return True, rows, (f"verified: {outcome.pages} page(s), {len(rows)} records, IDs unique, links answer")
