"""Rank search candidates before any page is opened.

Scores combine trust (your preferred sites, sites that proved themselves in
this track, sites the planner suggests), signals in the search result itself,
rotation (unread pages first) and reputation (sites that blocked us recently).
Nothing here names a topic or a site: specific sites only enter through the
user's lists, the planner's suggestions and recorded history.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from urllib.parse import urlparse

from vora.extraction.files import file_extension
from vora.extraction.semantics import ADDRESS_WORDS, compact_tokens, content_tokens, tokens
from vora.shared.contracts import GoalPlan
from vora.shared.urls import same_site

from vora.research.discovery.discovery import SearchResult

PREFERRED = 100.0
PROVEN = 40.0
SUGGESTED = 25.0
UNREAD = 20.0
UNPRODUCTIVE = -40.0
READ_RECENTLY = -150.0  # outweighs every boost: within the revisit window, unread pages first
BLOCKED = -60.0
SITE_NAME = 80.0        # the site's own name is a name the user mentioned ("from example"): it must lead
CODE_HOST = -150.0      # a code repository is not a data source unless the request is about software
KNOWN_GOOD = 12.0       # gave data in earlier research (any track, or another installation)
KNOWN_BLOCKING = -25.0  # blocked automated reading more often than it gave data
RANK_STEP = 1.5
DATA_WORDS = frozenset({"data", "dataset", "statistic", "table", "chart", "database", "series", "index",
                        "report", "figure", "survey", "census", "list", "annual", "monthly"})
CODE_HOSTS = frozenset({"github.com", "gitlab.com", "bitbucket.org", "pypi.org", "npmjs.com", "sourceforge.net",
                        "githubusercontent.com", "huggingface.co", "kaggle.com"})
AUTHORITY_LABELS = frozenset({"gov", "gouv", "gob", "govt", "edu", "ac", "int", "mil"})


@dataclass(slots=True)
class RankedCandidate:
    result: SearchResult
    score: float
    reasons: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return self.result.url

    @property
    def domain(self) -> str:
        return (urlparse(self.result.url).hostname or "").removeprefix("www.")


def _registry_domains(plan: GoalPlan) -> list[str]:
    """Domains of the registry sources the request names (empty when it names none)."""
    if not plan.registry_sources:
        return []
    from vora.learning.source_registry import by_id

    return [domain for entry in filter(None, map(by_id, plan.registry_sources)) for domain in entry.domains]


def _match(host: str, domains: list[str]) -> str | None:
    return next((domain for domain in domains if same_site(host, domain)), None)


def _age(timestamp: str) -> str:
    try:
        days = (datetime.now(UTC) - datetime.fromisoformat(timestamp)).days
    except ValueError:
        return "recently"
    return "today" if days == 0 else f"{days} day{'s' if days != 1 else ''} ago"


def _hours_since(timestamp: str) -> float:
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(timestamp)).total_seconds() / 3600
    except ValueError:
        return float("inf")


def site_name_match(host: str, mentions: list[str]) -> str | None:
    """The mentioned name a host's own name matches ("example.gov.in" ~ "example"), if any.

    A name matches a whole label of the host (or a hyphen-separated piece of
    one), exactly or with a typo. A site that merely contains a word ("indiadata"
    for "india") does not match.
    """
    pieces = {piece for label in host.split(".") for piece in [label, *re.split(r"[-_]", label)] if piece}
    for mention in mentions:
        name = re.sub(r"[^a-z0-9]+", "", mention.casefold())
        if len(name) < 3:
            continue
        for piece in pieces:
            if piece == name or (len(name) >= 5 and piece[:1] == name[:1]
                                 and SequenceMatcher(None, piece, name).ratio() >= 0.9):
                return mention
    return None


def read_recently(url: str, signals: dict, revisit_hours: float) -> bool:
    """True when this track read the page within the revisit window."""
    key = url.rstrip("/")
    entry = next((value for seen, value in signals.get("visited", {}).items() if seen.rstrip("/") == key), None)
    return entry is not None and _hours_since(entry["at"]) < revisit_hours


def score_candidate(result: SearchResult, plan: GoalPlan, preferred: list[str],
                    signals: dict, revisit_hours: float = 24) -> RankedCandidate:
    host = (urlparse(result.url).hostname or "").removeprefix("www.")
    score, reasons = 0.0, []

    if result.origin == "registry":
        score += PREFERRED + 20
        reasons.append(f"Official source for '{plan.source_mentions[0] if plan.source_mentions else host}'")
    if result.origin == "resolved":
        score += PREFERRED + 5 - result.rank
        reasons.append("Official site for your request")
    if result.origin == "goal":
        score += PREFERRED + 10
        reasons.append("Link in your goal")
    if domain := _match(host, preferred):
        score += PREFERRED
        reasons.append(f"Preferred source ({domain})")
    proven = next((rows for domain, rows in signals.get("proven", {}).items() if same_site(host, domain)), 0)
    if proven:
        score += PROVEN
        reasons.append(f"Proven: {proven:,} accepted rows in this track")
    named = site_name_match(host, plan.source_mentions)
    official = _registry_domains(plan)
    if named and official and not _match(host, official):
        # The registry knows which site the name means ("example" is example.gov.in), so other sites
        # that happen to share the name (a state's or another country's gazette) are not it.
        named = None
    if named:
        score += SITE_NAME
        reasons.append(f"Site name matches '{named}'")
    known = next((counts for domain, counts in signals.get("known", {}).items() if same_site(host, domain)), None)
    if known and not proven and known.get("useful_reads"):
        score += KNOWN_GOOD
        reasons.append(f"Gave data in earlier research ({known.get('accepted_rows', 0):,} rows)")
    if _match(host, plan.suggested_sources):
        score += SUGGESTED
        reasons.append("Suggested by the planner")

    page_text = f"{result.title} {result.snippet} {urlparse(result.url).path}"
    # Plain tokens carry the data-page signal ("list", "data" are stopwords elsewhere);
    # joined pairs let a name written as one word match ("eExample" ~ example).
    words = set(tokens(page_text)) | compact_tokens(page_text)
    data_hits = len(words & DATA_WORDS)
    authority = bool(set(host.split(".")[1:]) & AUTHORITY_LABELS)
    data_score = min(15.0, 4.0 * data_hits + (5.0 if authority else 0.0))
    if data_score:
        score += data_score
        reasons.append("Looks like a data page" if data_hits else "Official or academic publisher")

    wanted = {token for term in [*plan.subject_terms,
                                 *(name for concept in plan.required_concepts if concept.kind != "time"
                                   for name in [concept.name, *concept.aliases])]
              for token in content_tokens(term) if token not in ADDRESS_WORDS}
    topic_hits = len(wanted & words)
    if topic_hits:
        score += min(10.0, 4.0 * topic_hits)
        reasons.append("Mentions your subject")

    # Rotation: unread pages first. A useful page is read again only after the
    # revisit window, so later runs reach the queue instead of re-reading winners.
    visited = signals.get("visited", {})
    key = result.url.rstrip("/")
    previous = next((entry for url, entry in visited.items() if url.rstrip("/") == key), None)
    if previous is None:
        score += UNREAD
        reasons.append("Not read before in this track")
    elif previous["accepted"] == 0:
        score += UNPRODUCTIVE
        reasons.append("Read before without useful rows")
    elif _hours_since(previous["at"]) < revisit_hours:
        score += READ_RECENTLY
        reasons.append(f"Read {_age(previous['at'])}; unread pages first")
    else:
        reasons.append(f"Refreshing (last read {_age(previous['at'])})")

    blocked_at = next((at for domain, at in signals.get("blocked", {}).items() if same_site(host, domain)), None)
    if blocked_at:
        score += BLOCKED
        reasons.append(f"Blocked us {_age(blocked_at)}")
    elif known and known.get("blocked_reads", 0) > known.get("useful_reads", 0):
        score += KNOWN_BLOCKING
        reasons.append(f"Often blocks automated reading ({known['blocked_reads']} of {known.get('reads', 0)} reads)")

    if host in CODE_HOSTS or any(host.endswith("." + code) for code in CODE_HOSTS):
        software = {"code", "repo", "repository", "software", "library", "package", "commit", "api", "sdk", "github"}
        if not (software & set(tokens(" ".join([plan.normalized_goal, *plan.subject_terms])))):
            score += CODE_HOST
            reasons.append("A code repository, not a data publisher")

    score -= RANK_STEP * result.rank
    return RankedCandidate(result, round(score, 2), reasons)


def rank(results: list[SearchResult], plan: GoalPlan, preferred: list[str], signals: dict,
         *, max_per_domain: int = 1, revisit_hours: float = 24, max_named_pages: int = 8) -> list[RankedCandidate]:
    """Best candidates first. Files are left out (they are registered, not rendered).

    Diversity: at most ``max_per_domain`` pages per site (preferred sites get
    one extra) appear before any site's further pages.
    """
    pages = [result for result in results if not file_extension(result.url)]
    ranked = sorted((score_candidate(result, plan, preferred, signals, revisit_hours) for result in pages),
                    key=lambda item: item.score, reverse=True)
    first, later, per_domain = [], [], {}
    for item in ranked:
        official = _registry_domains(plan)
        named = (bool(_match(item.domain, official)) if official
                 else bool(site_name_match(item.domain, plan.source_mentions)))             or item.result.origin in {"goal", "registry", "resolved"}
        # The site the user named may fill the batch; any other site gets its share.
        allowance = max(max_per_domain, max_named_pages) if named \
            else max_per_domain + (1 if _match(item.domain, preferred) else 0)
        count = per_domain.get(item.domain, 0)
        per_domain[item.domain] = count + 1
        if count < allowance:
            first.append(item)
        else:
            item.reasons.append(f"Another page from {item.domain} was ranked higher")
            later.append(item)
    return first + later
