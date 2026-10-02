"""Interactive exploration of a page: reveal data a plain render does not show.

A plain render reads the page as it first appears. Many pages keep their
numbers behind interaction: lazy loading on scroll, collapsed sections, tabs,
"show more" / "load more" buttons, further pages of a list, iframes, shadow
DOM, and charts that fetch their data as JSON. ``explore`` performs a small,
bounded set of *safe* interactions and returns every distinct state of the
page (each as an ``ExecutionResult``), plus the JSON/CSV responses the page
loaded.

Safety: links to other sites are never followed, and controls whose text
suggests an account, purchase, download or destructive action are skipped
(``is_safe_action``). Forms are operated only when they are *query* forms
(search, filter, date range; see ``vora.browser.forms``): login, payment, contact,
upload, subscribe and CAPTCHA forms are never touched. Every step is bounded by
count and by time.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from vora.browser.contracts import ExecutionResult
from vora.browser.events import LifecycleEvent
from vora.browser.forms import INVENTORY_JS, FormInfo, classify_form, parse_forms, plan_fills, submit_control

if TYPE_CHECKING:
    from vora.browser.engine import BrowserEngine

MAX_ACTIONS = 25
MAX_SCROLLS = 10
MAX_EXPAND = 15
MAX_TABS = 10
MAX_MORE_CLICKS = 5
MAX_PAGES = 3
MAX_FORMS = 2
MAX_FRAMES = 5
MAX_CAPTURED = 20
MAX_CAPTURED_BYTES = 2_000_000
CLICK_TIMEOUT_MS = 3000
# How long to wait for a click to start a page load before treating it as an
# in-page update (an AJAX pager, an expanded section).
NAV_WAIT_MS = 2500
SETTLE_TIMEOUT_MS = 2000

_UNSAFE = re.compile(
    r"\b(?:log ?in|log ?out|sign ?(?:in|up|out)|register|subscribe|buy|cart|basket|checkout|order|"
    r"purchase|download|delete|remove|pay|payment|donate|apply|submit|book now|reserve|contact|share|"
    r"print|e-?mail|call|whatsapp|facebook|twitter|linkedin|instagram|youtube|install|upgrade|trial)\b",
    re.I,
)
_FILE_LINK = re.compile(r"\.(?:pdf|zip|exe|msi|dmg|apk|docx?|xlsx?|pptx?|csv|mp4|mp3)(?:$|[?#])", re.I)


_PAGER_POSTBACK = re.compile(r"__doPostBack\(.*Page\$", re.I)


def is_safe_action(text: str, href: str, page_url: str, *, in_form: bool = False,
                   input_type: str = "") -> bool:
    """Whether clicking a control can only change what the page shows.

    Refuses controls inside forms that are not query forms, submit buttons,
    links to other sites or to files, and anything whose label suggests an
    account, purchase, download, messaging or destructive action. A grid's
    page link (``__doPostBack('grid','Page$2')``) only changes the visible
    page, so it is allowed even when the whole page is one form.
    """
    if in_form and not _PAGER_POSTBACK.search(href or ""):
        return False
    if input_type.casefold() in {"submit", "reset", "file", "image"}:
        return False
    label = " ".join(str(text).split())
    if not label or len(label) > 80 or _UNSAFE.search(label):
        return False
    href = (href or "").strip()
    if href and not href.startswith(("#", "javascript:")):
        target = urlparse(urljoin(page_url, href))
        if target.scheme not in {"http", "https"} or _FILE_LINK.search(target.path):
            return False
        if _host(target.netloc) != _host(urlparse(page_url).netloc):
            return False
    return True


def _host(netloc: str) -> str:
    return netloc.casefold().split(":")[0].removeprefix("www.")


@dataclass(frozen=True, slots=True)
class CapturedResponse:
    """A JSON or CSV response the page loaded (typically a chart's data)."""

    url: str
    content_type: str
    body: bytes

    @property
    def extension(self) -> str:
        return "csv" if "csv" in self.content_type or self.url.lower().split("?")[0].endswith(".csv") else "json"


@dataclass(frozen=True, slots=True)
class ExploreHints:
    """What the request asks for, so a query form can be filled from it.

    Nothing is guessed: with no window and no keywords a form keeps the
    site's own defaults (and a lone search box is left alone).
    """

    window: tuple[date, date] | None = None
    keywords: tuple[str, ...] = ()
    places: tuple[str, ...] = ()
    forms: bool = True


@dataclass(slots=True)
class Exploration:
    states: list[ExecutionResult] = field(default_factory=list)
    captured: list[CapturedResponse] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)


# Marks candidate controls with a data attribute and describes them.
_LIST_CONTROLS = r"""
(kind) => {
  const main = document.querySelector('main, [role=main], article') || document.body;
  const visible = (el) => {
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return box.width > 0 && box.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  };
  const chrome = (el) => el.closest('nav, header, footer, aside, [role=navigation], [role=banner], [role=contentinfo]');
  const label = (el) => (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
  let nodes = [];
  if (kind === 'consent') {
    nodes = [...document.querySelectorAll('button, [role=button], a')].filter((el) =>
      /^(accept|accept all|accept cookies|i accept|agree|i agree|allow|allow all|ok|got it)\b/i.test(label(el)) &&
      el.closest('[id*=cookie i], [class*=cookie i], [id*=consent i], [class*=consent i], [id*=gdpr i], [class*=gdpr i], [aria-label*=cookie i], [aria-label*=consent i]'));
  } else if (kind === 'tab') {
    nodes = [...document.querySelectorAll('[role=tab]')].filter((el) => el.getAttribute('aria-selected') !== 'true');
  } else if (kind === 'expand') {
    nodes = [...main.querySelectorAll('[aria-expanded=false]')].filter((el) => !chrome(el));
  } else if (kind === 'more') {
    nodes = [...main.querySelectorAll('button, a, [role=button]')].filter((el) =>
      /\b(show|load|view|see)\s+(more|all)\b|\bmore results\b|\bexpand all\b/i.test(label(el)));
  } else if (kind === 'next') {
    nodes = [...document.querySelectorAll('a[rel=next], [class*=pagination i] a, [class*=pager i] a, nav a')].filter((el) =>
      el.getAttribute('rel') === 'next' || /^(next|next page|›|»|>)$/i.test(label(el)) ||
      (/^\d{1,3}$/.test(label(el)) && /Page\$|[?&]page=/i.test(el.getAttribute('href') || '')));
  }
  return nodes.filter(visible).slice(0, 30).map((el, index) => {
    const id = `${kind}-${Date.now()}-${index}`;
    el.setAttribute('data-vora-id', id);
    const form = el.closest('form');
    return { id, text: label(el).slice(0, 100), href: el.getAttribute('href') || '',
             formIndex: form ? [...document.forms].indexOf(form) : -1,
             type: (el.getAttribute('type') || '').toLowerCase() };
  });
}
"""

# Copies open shadow roots into ordinary markup so the HTML parser sees them.
_FLATTEN_SHADOW = r"""
() => {
  document.querySelectorAll('[data-vora-shadow]').forEach((node) => node.remove());
  let count = 0;
  const walk = (root) => root.querySelectorAll('*').forEach((el) => {
    if (el.shadowRoot && count < 50) {
      const box = document.createElement('div');
      box.setAttribute('data-vora-shadow', el.tagName.toLowerCase());
      box.innerHTML = el.shadowRoot.innerHTML;
      document.body.appendChild(box);
      count += 1;
      walk(el.shadowRoot);
    }
  });
  if (document.body) walk(document);
  return count;
}
"""

_OPEN_DETAILS = r"""
() => {
  const closed = [...document.querySelectorAll('details:not([open])')].slice(0, 30);
  closed.forEach((node) => { node.open = true; });
  return closed.length;
}
"""

_SCROLL = r"""
() => {
  window.scrollBy(0, Math.max(800, window.innerHeight));
  return window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 4;
}
"""

_TEXT_SIGNATURE = "() => document.body ? document.body.innerText.slice(0, 50000) : ''"


class _Session:
    """One exploration: the page, its time budget and the states seen so far."""

    def __init__(self, engine: "BrowserEngine", page: Any, url: str, execution_id: str, budget: float,
                 max_actions: int, should_stop: Callable[[], bool] | None,
                 hints: ExploreHints | None = None) -> None:
        self.engine, self.page, self.url, self.execution_id = engine, page, url, execution_id
        self.hints = hints or ExploreHints()
        self._forms: list[FormInfo] | None = None
        self.deadline = time.monotonic() + budget
        self.max_actions = max_actions
        self.should_stop = should_stop
        self.started = time.monotonic()
        self.status: int | None = None
        self.exploration = Exploration()
        self._signatures: set[str] = set()

    def stopped(self) -> bool:
        return (len(self.exploration.actions) >= self.max_actions or time.monotonic() >= self.deadline
                or bool(self.should_stop and self.should_stop()))

    def settle(self) -> None:
        try:
            self.page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
        except Exception:
            pass

    def _read(self) -> tuple[str, str]:
        self.page.evaluate(_FLATTEN_SHADOW)
        return self.page.evaluate(_TEXT_SIGNATURE), self.page.content()

    def record(self, label: str, html: str | None = None, url: str | None = None, title: str | None = None) -> bool:
        """Keep the current state if its visible text is new. Returns whether it was kept."""
        try:
            if html is None:
                try:
                    signature_text, html = self._read()
                except Exception:
                    # The page was mid-navigation (a click just started a load): wait, read again.
                    self.page.wait_for_load_state("domcontentloaded", timeout=CLICK_TIMEOUT_MS * 3)
                    self.settle()
                    signature_text, html = self._read()
            else:
                signature_text = html
        except Exception:
            return False
        signature = hashlib.sha256(signature_text.encode("utf-8", "replace")).hexdigest()
        if signature in self._signatures:
            return False
        self._signatures.add(signature)
        state_id = f"{self.execution_id}-{len(self.exploration.states)}"
        result = ExecutionResult(
            execution_id=state_id, requested_url=self.url, final_url=url or self.page.url,
            title=title if title is not None else self.page.title(), html=html, status=self.status,
            elapsed_seconds=round(time.monotonic() - self.started, 6), network_idle_reached=True,
            metadata=MappingProxyType({"headless": self.engine.settings.headless,
                                       "fetched_at": datetime.now(UTC).isoformat(), "state": label,
                                       "response_headers": MappingProxyType({})}),
        )
        self.exploration.states.append(result)
        self.engine._emit(LifecycleEvent.EXECUTION_COMPLETE, state_id, result=result)
        return True

    def forms(self) -> list[FormInfo]:
        """The page's forms (cached until the page changes)."""
        if self._forms is None:
            try:
                self._forms = parse_forms(self.page.evaluate(INVENTORY_JS))
            except Exception:
                self._forms = []
        return self._forms

    def in_other_form(self, index: int) -> bool:
        """Whether a control sits in a form that is not a query form."""
        if index < 0:
            return False
        forms = self.forms()
        return not (index < len(forms) and classify_form(forms[index]) == "query")

    def controls(self, kind: str) -> list[dict[str, Any]]:
        try:
            found = self.page.evaluate(_LIST_CONTROLS, kind)
        except Exception:
            return []
        return [item for item in found
                if kind == "consent" or is_safe_action(item["text"], item["href"], self.page.url,
                                                       in_form=self.in_other_form(item["formIndex"]),
                                                       input_type=item["type"])]

    def search_forms(self) -> list[FormInfo]:
        """Query forms worth submitting: real search forms that post to this site."""
        found = []
        for form in self.forms():
            if not form.visible or classify_form(form) != "query" or submit_control(form) is None:
                continue
            action = urlparse(urljoin(self.page.url, form.action or self.page.url))
            if _host(action.netloc) != _host(urlparse(self.page.url).netloc):
                continue
            filled = plan_fills(form, window=self.hints.window, keywords=list(self.hints.keywords),
                                places=list(self.hints.places))
            # A lone search box with nothing to search for is site chrome, not a listing.
            if filled or len(form.controls) >= 2:
                found.append(form)
        return sorted(found, key=lambda form: -len(form.controls))[:MAX_FORMS]

    def submit(self, form: FormInfo) -> str | None:
        """Fill a query form from the request and submit it. Returns what was searched, or None."""
        button = submit_control(form)
        if button is None or self.stopped():
            return None
        fills = plan_fills(form, window=self.hints.window, keywords=list(self.hints.keywords),
                           places=list(self.hints.places))
        try:
            for fill in fills:
                target = self.page.locator(f'[data-vora-field="{fill.field_id}"]').first
                if fill.kind == "select":
                    target.select_option(label=fill.value, timeout=CLICK_TIMEOUT_MS)
                else:
                    target.fill(fill.value, timeout=CLICK_TIMEOUT_MS)
        except Exception:
            return None
        if not self._press(self.page.locator(f'[data-vora-field="{button.id}"]').first, expect_navigation=True):
            return None
        self._forms = None
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=CLICK_TIMEOUT_MS * 3)
        except Exception:
            pass
        self.settle()
        summary = ", ".join(f"{fill.value} ({fill.why})" for fill in fills) or "the site's default filters"
        note = f"searched a query form with {summary}"
        self.exploration.actions.append(note)
        return note

    def _press(self, locator: Any, expect_navigation: bool = False) -> bool:
        """Click a locator. When the click may load a page (a postback link, a submit
        button), wait for that load, so the next read is of the new page, not the old.
        Returns False when the click itself failed."""
        clicked = False
        try:
            if expect_navigation:
                with self.page.expect_navigation(timeout=NAV_WAIT_MS):
                    locator.click(timeout=CLICK_TIMEOUT_MS)
                    clicked = True
            else:
                locator.click(timeout=CLICK_TIMEOUT_MS)
                clicked = True
        except Exception:
            # No load followed the click: the page changed in place (an AJAX update).
            return clicked
        return True

    def click(self, control: dict[str, Any], note: str) -> bool:
        """Click a marked control; come back if it left the site."""
        if self.stopped():
            return False
        before = urlparse(self.page.url).netloc
        if not self._press(self.page.locator(f'[data-vora-id="{control["id"]}"]').first,
                           expect_navigation=control["href"].startswith("javascript:")):
            return False
        self.exploration.actions.append(note)
        self._forms = None
        self.settle()
        if _host(urlparse(self.page.url).netloc) != _host(before):
            try:
                self.page.go_back(timeout=CLICK_TIMEOUT_MS * 3)
                self.settle()
            except Exception:
                pass
            return False
        return True


def paginate(session: "_Session", label: str) -> None:
    """Follow "next" and numbered page links: by address, or by clicking postback pagers."""
    page, visited, seen_numbers = session.page, {session.page.url.split("#")[0]}, {1}
    for number in range(2, MAX_PAGES + 2):
        if session.stopped():
            return
        controls = session.controls("next")
        following = next((item for item in controls if not re.fullmatch(r"\d{1,3}", item["text"])), None)
        if following is None:  # numbered links only: the lowest page not opened yet
            numbered = [item for item in controls if re.fullmatch(r"\d{1,3}", item["text"])
                        and int(item["text"]) not in seen_numbers]
            following = min(numbered, key=lambda item: int(item["text"]), default=None)
        if following is None:
            return
        if re.fullmatch(r"\d{1,3}", following["text"]):
            seen_numbers.add(int(following["text"]))
        href = following["href"]
        if href and not href.startswith(("#", "javascript:")):
            target = urljoin(page.url, href).split("#")[0]
            if target in visited:
                return
            visited.add(target)
            try:
                page.goto(target, wait_until="domcontentloaded",
                          timeout=session.engine.settings.navigation_timeout_ms)
            except Exception:
                return
            session.exploration.actions.append(f"opened {label} {number}")
            session._forms = None
            session.settle()
        elif not session.click(following, f"opened {label} {number}"):
            return
        if not session.record(f"{label} {number}"):
            return  # nothing new appeared: the last page


def explore(engine: "BrowserEngine", url: str, *, budget_seconds: float = 30.0, max_actions: int = MAX_ACTIONS,
            should_stop: Callable[[], bool] | None = None, hints: ExploreHints | None = None,
            events=None) -> Exploration:
    """Open ``url`` and reveal what a plain render misses. See the module docstring."""
    from vora.browser.engine import new_context

    execution_id = uuid.uuid4().hex
    context = new_context(engine)
    pending: list[Any] = []

    def on_response(response: Any) -> None:
        if len(pending) >= MAX_CAPTURED or response.request.resource_type not in {"xhr", "fetch"}:
            return
        content_type = response.headers.get("content-type", "").casefold()
        if "json" not in content_type and "csv" not in content_type:
            return
        length = response.headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_CAPTURED_BYTES:
            return
        pending.append(response)

    def drain() -> None:
        """Read the bodies of captured responses (while their document is loaded)."""
        while pending:
            item = pending.pop(0)
            try:
                body = item.body()
            except Exception:
                continue
            if 0 < len(body) <= MAX_CAPTURED_BYTES and len(session.exploration.captured) < MAX_CAPTURED:
                session.exploration.captured.append(CapturedResponse(
                    item.url, item.headers.get("content-type", "").casefold(), body))

    engine._emit(LifecycleEvent.EXECUTION_START, execution_id, url=url)
    try:
        page = context.new_page()
        page.set_default_timeout(engine.settings.navigation_timeout_ms)
        page.on("response", on_response)
        response = page.goto(url, wait_until="domcontentloaded", timeout=engine.settings.navigation_timeout_ms)
        session = _Session(engine, page, url, execution_id, budget_seconds, max_actions, should_stop, hints)
        session.status = response.status if response is not None else None
        session.settle()

        for control in session.controls("consent")[:1]:
            session.click(control, "accepted the cookie banner")

        for _ in range(MAX_SCROLLS):
            if session.stopped():
                break
            try:
                at_bottom = page.evaluate(_SCROLL)
            except Exception:
                break
            page.wait_for_timeout(300)
            if at_bottom:
                break
        session.settle()
        try:
            opened = page.evaluate(_OPEN_DETAILS)
            if opened:
                session.exploration.actions.append(f"opened {opened} collapsed sections")
        except Exception:
            pass
        session.record("scrolled and expanded")

        for control in session.controls("expand")[:MAX_EXPAND]:
            if not session.click(control, f"expanded '{control['text'][:40]}'"):
                continue
        session.record("expanded sections")

        for control in session.controls("tab")[:MAX_TABS]:
            if session.click(control, f"opened tab '{control['text'][:40]}'"):
                session.record(f"tab: {control['text'][:40]}")

        for count in range(1, MAX_MORE_CLICKS + 1):
            more = session.controls("more")
            if not more or not session.click(more[0], f"clicked '{more[0]['text'][:40]}'"):
                break
            session.record(f"{more[0]['text'][:30]} ×{count}")

        # Embedded content first: moving to a further page drops these frames.
        frames = [frame for frame in page.frames[1:] if frame.url.startswith(("http", "about:srcdoc"))]
        for frame in frames[:MAX_FRAMES]:
            if session.stopped():
                break
            try:
                html = frame.content()
            except Exception:
                continue
            if len(re.sub(r"<[^>]+>", " ", html).split()) < 12:
                continue  # empty or decorative frame
            frame_url = frame.url if frame.url.startswith("http") else page.url
            session.record(f"embedded: {urlparse(frame_url).netloc}", html=html, url=frame_url,
                           title=f"{page.title()} (embedded content)")

        # Bodies of this document's responses are gone once another page loads.
        drain()
        paginate(session, "page")

        # Portals keep their records behind search forms: run the query forms the request
        # can fill (never login, payment, contact, upload, subscribe or CAPTCHA forms).
        if session.hints.forms:
            for form in session.search_forms():
                if session.stopped():
                    break
                if session.submit(form):
                    session.record("search results")
                    drain()
                    paginate(session, "results page")
                    break  # results replace the page; the forms are gone
        drain()
        return session.exploration
    except Exception as exc:
        engine._emit(LifecycleEvent.EXECUTION_FAILED, execution_id, url=url,
                     error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        context.close()
