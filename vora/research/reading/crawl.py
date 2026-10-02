"""Focused crawl: which same-site links from a page are worth opening.

Instead of visiting every link of a site, a page's links are ranked by how
well their text and address match the request (its subject words, requested
concepts and years). Only the best few are opened, one level deep.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from vora.extraction.files import file_extension
from vora.extraction.semantics import content_tokens, similar
from vora.shared.contracts import GoalPlan
from vora.shared.urls import same_site

# Paths that are about the site or the visitor, never the data.
_BOILERPLATE = re.compile(
    r"(?:^|[/_-])(?:login|log-in|signin|sign-in|signup|sign-up|register|account|profile|cart|basket|checkout|"
    r"privacy|terms|cookies?|contact|about|careers?|jobs|help|faq|support|subscribe|newsletter|"
    r"advertise|press|legal|sitemap|search|tag|tags|author|share|login\.php)(?:$|[/._?-])",
    re.I,
)
_DATA_WORDS = frozenset({"data", "statistic", "table", "report", "list", "chart", "dataset", "figure",
                         "annual", "monthly", "yearly", "historical", "archive", "series"})
MIN_SCORE = 1.0


@dataclass(frozen=True, slots=True)
class SiteLink:
    url: str
    text: str
    score: float


def _terms(plan: GoalPlan) -> tuple[set[str], set[str]]:
    words: set[str] = set()
    for term in [*plan.subject_terms, *plan.subject_heads, *plan.geography, *plan.entities]:
        words.update(content_tokens(term))
    for concept in plan.concepts:
        if concept.kind == "time":
            continue
        for name in [concept.name, *concept.aliases]:
            words.update(content_tokens(name))
    years: set[str] = set()
    if plan.time_scope:
        start, end = int(plan.time_scope.start[:4]), int(plan.time_scope.end[:4])
        years = {str(year) for year in range(start, end + 1)}
    return words, years


def rank_site_links(html: str, page_url: str, plan: GoalPlan, limit: int = 3,
                    exclude: set[str] | None = None) -> list[SiteLink]:
    """The ``limit`` best same-site links on a page for this request."""
    host = urlparse(page_url).hostname or ""
    words, years = _terms(plan)
    excluded = {url.rstrip("/") for url in (exclude or set())} | {page_url.split("#")[0].rstrip("/")}
    soup = BeautifulSoup(html, "html.parser")
    best: dict[str, SiteLink] = {}
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(page_url, href).split("#")[0]
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not same_site(parsed.hostname or "", host):
            continue
        key = url.rstrip("/")
        if key in excluded or _BOILERPLATE.search(parsed.path) or file_extension(url):
            continue
        text = " ".join(anchor.get_text(" ", strip=True).split())[:120]
        path_words = " ".join(re.split(r"[/_\-.?=&]+", parsed.path + " " + parsed.query))
        tokens = set(content_tokens(f"{text} {path_words}"))
        score = sum(1.0 for token in tokens if any(similar(token, word) >= 0.85 for word in words))
        score += 0.5 * len(years & set(re.findall(r"(?:19|20)\d{2}", f"{text} {parsed.path}")))
        score += 0.3 * len(tokens & _DATA_WORDS)
        if anchor.find_parent(["nav", "header", "footer"]) is not None:
            score -= 0.5  # site navigation: rarely the page with the numbers
        if score >= MIN_SCORE and (key not in best or best[key].score < score):
            best[key] = SiteLink(url=url, text=text, score=round(score, 2))
    return sorted(best.values(), key=lambda link: -link.score)[:limit]
