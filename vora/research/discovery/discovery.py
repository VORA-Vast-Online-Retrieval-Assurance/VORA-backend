"""Browser-rendered search discovery with deterministic URL handling."""

from __future__ import annotations

import base64
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from vora.settings import settings
from vora.browser.engine import BrowserEngine
from vora.extraction.noise import CONSENT_PATTERNS, is_challenge_page
from vora.extraction.semantics import topic_tokens
from vora.shared.contracts import GoalPlan
from vora.shared.urls import goal_domains, same_site
from vora.shared.regions import REGIONS

from vora.research.discovery import search_api
from vora.learning.source_registry import by_id as registry_entry


def _unwrap(href: str) -> str | None:
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    query = parse_qs(parsed.query)
    if "uddg" in query:
        return unquote(query["uddg"][0])
    if "bing.com" in parsed.netloc and query.get("u", [""])[0].startswith("a1"):
        value = query["u"][0][2:]
        try:
            return base64.b64decode(value + "=" * (-len(value) % 4)).decode()
        except Exception:
            return None
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return href.split("#", 1)[0]
    return None


def _absolute(url: str | None) -> str | None:
    """Only absolute http(s) links are candidates (decoded redirects can be anything)."""
    if not url:
        return None
    parsed = urlparse(url)
    return url if parsed.scheme in {"http", "https"} and parsed.netloc else None


_SEARCH_HOSTS = ("duckduckgo.com", "bing.com", "microsoft.com", "msn.com", "go.microsoft.com")


@dataclass(frozen=True, slots=True)
class SearchResult:
    url: str
    title: str = ""
    snippet: str = ""
    rank: int = 0                 # position within its query's results
    query: str = ""
    engine: str = ""
    origin: str = "search"        # goal | preferred | search


def _text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def parse_search_results(html: str, engine: str = "duckduckgo", fallback: bool = True) -> list[SearchResult]:
    """Result links with their title and snippet, in the engine's order.

    ``fallback`` allows an unknown layout to yield every outbound link; it is
    turned off for pages that look like a consent or verification screen.
    """
    soup = BeautifulSoup(html, "html.parser")
    blocks: list[tuple[str, str, str]] = []
    for block in soup.select("div.result, div.web-result"):  # DuckDuckGo HTML
        anchor = block.select_one("a.result__a")
        if anchor:
            blocks.append((anchor.get("href", ""), _text(anchor), _text(block.select_one(".result__snippet"))))
    for block in soup.select("li.b_algo"):  # Bing
        anchor = block.select_one("h2 a")
        if anchor:
            blocks.append((anchor.get("href", ""), _text(anchor),
                           _text(block.select_one(".b_caption p, p, .b_lineclamp2, .b_lineclamp3"))))
    if not blocks and fallback:  # unknown layout: every outbound link, titled by its text
        blocks = [(anchor.get("href", ""), _text(anchor), "") for anchor in soup.select("a[href]")]

    results, seen = [], set()
    for href, title, snippet in blocks:
        url = _absolute(_unwrap(urljoin("https://html.duckduckgo.com", href)))
        if not url:
            continue
        host = urlparse(url).netloc.lower()
        if any(domain in host for domain in _SEARCH_HOSTS):
            continue
        key = url.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        results.append(SearchResult(url=url, title=title[:200], snippet=snippet[:400], rank=len(results), engine=engine))
    return results


SEARCH_ENGINES = (
    ("duckduckgo", "https://html.duckduckgo.com/html/?q={query}"),
    ("bing", "https://www.bing.com/search?q={query}"),
)
MAX_PREFERRED_SEARCHES = 4
MAX_QUERIES = 4

def region_for(plan: GoalPlan) -> tuple[str, str] | None:
    """The search region when the plan names exactly one known country."""
    names = (place.casefold().strip() for place in plan.geography)
    found = {REGIONS[name] for name in names if name in REGIONS}
    return found.pop() if len(found) == 1 else None


def search_url(engine: str, template: str, query: str, region: tuple[str, str] | None) -> str:
    url = template.format(query=quote_plus(query))
    if region:
        country, ddg_region = region
        url += f"&kl={ddg_region}" if engine == "duckduckgo" else f"&cc={country}"
    return url


MAX_COOLDOWN_SECONDS = 4 * 3600


class SearchGate:
    """Rations browser searches so engines do not start treating us as a bot.

    * An engine that answered with a block, consent screen or unrelated results
      rests for ``VORA_SEARCH_COOLDOWN_MINUTES``, doubling on each repeated
      block (at most 4 hours); a normal answer resets it.
    * Searches are at least ``VORA_SEARCH_INTERVAL_SECONDS`` apart.
    * At most ``VORA_MAX_SEARCHES_PER_HOUR`` browser searches, across tracks.

    State is per server process and shared by every batch.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._resting: dict[str, tuple[float, int]] = {}  # engine -> (until, strikes)
            self._recent: deque[float] = deque()
            self._last = 0.0

    def resting(self, engine: str) -> float:
        """Seconds the engine still rests (0 if available)."""
        with self._lock:
            until, _ = self._resting.get(engine, (0.0, 0))
        return max(0.0, until - time.monotonic())

    def acquire(self, engine: str, deadline: float | None = None) -> str | None:
        """Wait for this engine's turn. Returns why it cannot search, or None."""
        rest = self.resting(engine)
        if rest:
            return f"resting after a block ({max(1, round(rest / 60))} min left)"
        with self._lock:
            now = time.monotonic()
            while self._recent and now - self._recent[0] > 3600:
                self._recent.popleft()
            if len(self._recent) >= settings.max_searches_per_hour:
                return f"hourly search limit reached ({settings.max_searches_per_hour})"
            slot = max(now, self._last + settings.search_interval_seconds)
            if deadline is not None and slot >= deadline:
                return "skipped (time budget)"
            self._last = slot
            self._recent.append(slot)
        if slot > now:
            time.sleep(slot - now)
        return None

    def status(self) -> dict:
        """What the gate currently allows, for the API and the Tools view."""
        with self._lock:
            now = time.monotonic()
            recent = sum(1 for moment in self._recent if now - moment <= 3600)
            resting = {engine: round(until - now) for engine, (until, _) in self._resting.items() if until > now}
            strikes = {engine: count for engine, (_, count) in self._resting.items()}
        return {
            "engines": [{"name": name, "resting_seconds": resting.get(name, 0), "blocks_in_a_row": strikes.get(name, 0)}
                        for name, _ in SEARCH_ENGINES],
            "searches_last_hour": recent, "max_searches_per_hour": settings.max_searches_per_hour,
            "interval_seconds": settings.search_interval_seconds,
            "cooldown_minutes": settings.search_cooldown_minutes,
        }

    def record(self, engine: str, blocked: bool) -> None:
        with self._lock:
            if not blocked:
                self._resting.pop(engine, None)
                return
            _, strikes = self._resting.get(engine, (0.0, 0))
            seconds = min(MAX_COOLDOWN_SECONDS, settings.search_cooldown_minutes * 60 * 2 ** strikes)
            self._resting[engine] = (time.monotonic() + seconds, strikes + 1)


search_gate = SearchGate()


# At least this share of a result page must be about the query; otherwise the
# engine is answering an automated search with unrelated results.
MIN_ON_TOPIC = 0.2


def on_topic(results: list[SearchResult], query: str) -> list[SearchResult] | None:
    """Results that share a word with the query, or None if too few do."""
    words = topic_tokens(re.sub(r"\bsite:\S+", " ", query))
    if not words or not results:
        return results
    kept = [item for item in results
            if words & topic_tokens(f"{item.title} {item.snippet} {urlparse(item.url).path} "
                                    f"{(urlparse(item.url).hostname or '').replace('.', ' ')}")]
    return kept if len(kept) >= max(1, MIN_ON_TOPIC * len(results)) else None


def _search(engine: BrowserEngine, query: str, region: tuple[str, str] | None = None,
            deadline: float | None = None, report: list[str] | None = None) -> list[SearchResult]:
    """Run one query: the search API when configured, else the browser engines.

    A browser engine is skipped for the next one when its page is a
    verification page, a consent screen, empty, or unrelated to the query.
    What happened is added to ``report``.
    """
    notes = report if report is not None else []
    provider = search_api.configured()
    if provider:
        try:
            found = [SearchResult(url=url, title=title[:200], snippet=snippet[:400], rank=index, engine=provider)
                     for index, (url, title, snippet) in enumerate(search_api.api_search(query, region))
                     if _absolute(url)]
        except Exception as exc:
            notes.append(f"{provider}: failed ({type(exc).__name__})")
        else:
            notes.append(f"{provider}: {len(found)} results")
            if found:
                return [replace(result, query=query) for result in found]
    for name, template in SEARCH_ENGINES:
        if deadline is not None and time.monotonic() >= deadline:
            notes.append(f"{name}: skipped (time budget)")
            break
        refused = search_gate.acquire(name, deadline)
        if refused:
            notes.append(f"{name}: skipped, {refused}")
            continue
        try:
            page = engine.execute(search_url(name, template, query, region))
        except Exception as exc:
            notes.append(f"{name}: failed ({type(exc).__name__})")
            continue
        text = BeautifulSoup(page.html, "html.parser").get_text(" ", strip=True)
        if is_challenge_page(page.title, text):
            notes.append(f"{name}: blocked by a verification page")
            search_gate.record(name, blocked=True)
            continue
        consent = bool(CONSENT_PATTERNS.search(text[:2000]))
        found = parse_search_results(page.html, name, fallback=not consent)
        if found:
            relevant = on_topic(found, query)
            if relevant is None:
                notes.append(f"{name}: blocked (results unrelated to the query)")
                search_gate.record(name, blocked=True)
                continue
            notes.append(f"{name}: {len(relevant)} results")
            search_gate.record(name, blocked=False)
            return [replace(result, query=query) for result in relevant]
        notes.append(f"{name}: consent screen" if consent else f"{name}: no results")
        if consent:
            search_gate.record(name, blocked=True)
    return []


_FILLER = {"data", "with", "that", "every", "happen", "from", "give", "fetch", "scrape", "find", "show", "list",
           "about", "this", "what", "which", "where", "when", "into", "have", "need", "want", "please", "official", "all", "the", "and", "for", "are", "get", "any", "its", "who", "how"}


def short_query(text: str, words: int = 6) -> str:
    """The first content words of a long request: what a site's own search can use ("all the data of every session of
    X debates" -> "session debates")."""
    kept = [word for word in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", text) if word.lower() not in _FILLER]
    return " ".join(dict.fromkeys(kept))[:80] if len(kept) <= words else " ".join(dict.fromkeys(kept[:words]))


MAX_SITE_SEARCHES = 6


def sub_queries(text: str, parts: int = 3) -> list[str]:
    """The subjects a request joins with 'and', 'or', '&' or commas, each cut to its content words."""
    chunks = [chunk for chunk in re.split(r"\band\b|\bor\b|&|,|;", text, flags=re.I) if chunk.strip()]
    found = [short_query(chunk) for chunk in chunks]
    found = [query for query in dict.fromkeys(found) if len(query) >= 6]
    return found[:parts] or [short_query(text)]


def discover(engine: BrowserEngine, plan: GoalPlan, limit: int, preferred: list[str] | None = None,
             on_query: Callable[[str], object] | None = None, deadline: float | None = None,
             report: list[str] | None = None, resolved: list[str] | None = None) -> list[SearchResult]:
    """Collect candidates: links in the goal, the sites a model named and the program verified (``resolved``), site searches on preferred domains,
    then the planner's queries. Ranking happens later (``vora.research.discovery.ranking``).

    Plan queries are interleaved (first result of each query, then the second
    of each, ...) so every query contributes different sites.
    """
    results: dict[str, SearchResult] = {}
    region = region_for(plan)

    def add(items: list[SearchResult], origin: str) -> None:
        for item in items:
            key = item.url.rstrip("/")
            if key not in results and len(results) < limit:
                results[key] = replace(item, origin=origin)

    def out_of_time() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    # Official sources the request names (the registry) come first: no search engine is needed for them.
    add([SearchResult(url=entry.entry, title=entry.name, rank=index)
         for index, entry in enumerate(filter(None, map(registry_entry, plan.registry_sources)))], "registry")

    direct = re.findall(r"https?://[^\s<>\]\[()]+", plan.normalized_goal)
    add([SearchResult(url=url.rstrip(".,"), title="Link in your goal", rank=index)
         for index, url in enumerate(dict.fromkeys(direct))], "goal")

    # A website written without "https://" ("example.com") is still a link in the goal.
    written = {urlparse(item.url).hostname or "" for item in results.values()}
    # An address the registry knows as another name for an official site ("example.com") is not opened:
    # the official site already is.
    misnamed = {alias.casefold() for entry in filter(None, map(registry_entry, plan.registry_sources))
                for alias in entry.aliases if "." in alias} - {domain for entry in filter(
                    None, map(registry_entry, plan.registry_sources)) for domain in entry.domains}
    add([SearchResult(url=f"https://{domain}/", title="Site named in your goal", rank=index)
         for index, domain in enumerate(goal_domains(plan.normalized_goal))
         if domain not in written and domain not in misnamed], "goal")

    add([SearchResult(url=f"https://{host}/" if "/" not in host else f"https://{host}", title="Official site for your request", rank=index)
         for index, host in enumerate(resolved or [])], "resolved")

    base_query = plan.search_queries[0] if plan.search_queries else plan.normalized_goal
    base_query = re.sub(r"https?://\S+", " ", base_query).strip()
    for domain in (preferred or [])[:MAX_PREFERRED_SEARCHES]:
        if len(results) >= limit or out_of_time():
            break
        query = f"site:{domain} {base_query}"
        if on_query:
            on_query(query)
        # Keep only results that really are on the preferred site.
        add([item for item in _search(engine, query, region, deadline, report)
             if same_site(urlparse(item.url).hostname or "", domain)][:3], "preferred")

    # The sections of each official site that hold the data (a home page rarely does): a search restricted to the site.
    # A request that joins several subjects ("A debates and B debates") is searched once per subject, so a site
    # that keeps each under its own portal returns each portal.
    site_searches = 0
    for host in [entry.split("/")[0] for entry in (resolved or [])][:3]:
        for part in sub_queries(base_query):
            if len(results) >= limit or out_of_time() or site_searches >= MAX_SITE_SEARCHES:
                break
            site_searches += 1
            query = f"site:{host} {part}"
            if on_query:
                on_query(query)
            add([item for item in _search(engine, query, region, deadline, report)
                 if same_site(urlparse(item.url).hostname or "", host)][:3], "resolved")

    per_query: list[list[SearchResult]] = []
    for query in plan.search_queries[:MAX_QUERIES]:
        # Every query costs a search; stop once there are enough candidates.
        if out_of_time() or len(results) + len({item.url for found in per_query for item in found}) >= limit:
            break
        if on_query:
            on_query(query)
        per_query.append(_search(engine, query, region, deadline, report))
    for position in range(max(map(len, per_query), default=0)):
        add([found[position] for found in per_query if position < len(found)], "search")
    return list(results.values())
