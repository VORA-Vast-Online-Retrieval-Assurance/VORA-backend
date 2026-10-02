"""Is a learned listing about what was asked? Judged from the request's distinctive words, not from the site's name.

A learned listing is a list of records found on a page. Its rows count as answers only when the page is about the
request: its site's own name is one of the request's distinctive words (the request names the site), or enough of its
rows mention one. Words that say where (a country, a demonym) or that every request has ("data", "official",
"government", "scrape") are not distinctive, so a country's general portal does not pass for a request that merely
mentions that country.
"""

from __future__ import annotations

import re

from vora.shared.contracts import GoalPlan, Observation

# Words that never make a page about a request.
GENERIC = {
    "data", "dataset", "datasets", "scrape", "scraping", "scraped", "fetch", "get", "give", "show", "find", "list",
    "all", "every", "each", "latest", "recent", "new", "official", "government", "govt", "gov", "public", "website",
    "websites", "site", "sites", "portal", "page", "pages", "online", "information", "info", "details", "record",
    "records", "database", "from", "with", "the", "and", "for", "that", "this", "last", "years", "year", "month",
    "months", "week", "weeks", "day", "days", "india", "indian", "indians", "national", "central", "state", "states",
}
MIN_ROW_SHARE = 0.25
PREFIX = 5


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.casefold())


def distinctive_terms(plan: GoalPlan) -> set[str]:
    """The request's words that point at a topic, with places and generic words removed."""
    skip = set(GENERIC)
    for place in [*plan.geography, *getattr(plan, "regions", [])]:
        skip.update(_words(str(place)))
    sources = [*plan.subject_terms, *plan.entities, plan.normalized_goal]
    return {word for source in sources for word in _words(str(source)) if len(word) >= 3 and word not in skip}


def _matches(term: str, word: str) -> bool:
    return term == word or (len(term) >= PREFIX and len(word) >= PREFIX and term[:PREFIX] == word[:PREFIX])


def on_topic(plan: GoalPlan, host: str, rows: list[Observation]) -> tuple[bool, str]:
    """(whether the rows may count as answers, a reason when they may not)."""
    terms = distinctive_terms(plan)
    if not terms or not rows:
        return True, ""
    site = {piece for label in host.removeprefix("www.").split(".") for piece in [label, *re.split(r"[-_]", label)] if piece}
    if any(_matches(term, piece) for term in terms for piece in site):
        return True, ""                                   # the site's own name is what the request is about
    hits = 0
    for row in rows:
        text = " ".join(str(value) for value in row.fields.values())
        words = set(_words(text))
        if any(_matches(term, word) for term in terms for word in words):
            hits += 1
    share = hits / len(rows)
    if share >= MIN_ROW_SHARE:
        return True, ""
    return False, (f"Left out: {host} is not about this request (only {hits} of {len(rows)} records mention "
                   f"{', '.join(sorted(terms)[:4])})")
