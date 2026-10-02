"""Known official sources, kept as data (``data/sources.json``).

When a request names a portal ("example", "Example Gazette", "example.gov.in"), VORA should not
depend on a search engine to find it: engines rate-limit automated searches and rank blogs above
official sites. The registry maps the names people use to the site's own address, and may carry a
recipe (``vora.learning.recipes``) that reads the site's listing directly.

Nothing here names a site: every portal is an entry in the data file. Adding one is editing data.
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from vora.settings import settings
from vora.learning.recipes import Recipe
from vora.extraction.semantics import topic_tokens
from vora.shared.urls import goal_domains, same_site

logger = logging.getLogger("vora.registry")


class SourceEntry(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    domains: list[str] = Field(min_length=1)
    entry: str = Field(pattern=r"^https://")
    record_type: str | None = None
    recipe: Recipe | None = None


def _phrase(text: str) -> str:
    """Letters and digits only, words joined: "e-Example of India" -> "exampleofindia"."""
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def load(path: str | None = None) -> tuple[SourceEntry, ...]:
    """The registry's entries; a bad entry is logged and skipped, never fatal. The file is re-read whenever it
    changes, so an edit (by hand or by the periodic sync) takes effect without restarting the server."""
    file = Path(path or settings.source_registry_path)
    try:
        stamp = file.stat().st_mtime_ns
    except OSError:
        stamp = 0
    return _load(str(file), stamp)


@lru_cache(maxsize=8)
def _load(path: str, stamp: int) -> tuple[SourceEntry, ...]:
    file = Path(path)
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    except (OSError, ValueError) as exc:
        logger.warning("Source registry %s could not be read: %s", file, exc)
        return ()
    entries = []
    for item in payload.get("sources", []):
        try:
            entries.append(SourceEntry.model_validate(item))
        except ValidationError as exc:
            logger.warning("Skipping registry entry %s: %s", item.get("id", "?"), exc.errors()[:1])
    return tuple(entries)


load.cache_clear = _load.cache_clear      # type: ignore[attr-defined]


def _alias_hit(goal: str, alias: str) -> bool:
    """Whether the request mentions ``alias``, however it is spaced, hyphenated or capitalised."""
    wanted = _phrase(alias)
    if len(wanted) < 4:
        return False
    words = [_phrase(word) for word in re.findall(r"[A-Za-z0-9.]+", goal)]
    words = [word for word in words if word]
    # Any run of consecutive words, joined, equal to the alias ("e example" == "example").
    for start in range(len(words)):
        joined = ""
        for word in words[start:start + 6]:
            joined += word
            if joined == wanted:
                return True
            if len(joined) >= len(wanted):
                break
    return False


def match(goal: str, mentions: list[str] | None = None) -> list[SourceEntry]:
    """Registry entries the request names: by alias, by a domain written in it, or by a mentioned name."""
    found: list[SourceEntry] = []
    domains = goal_domains(goal)
    topics = topic_tokens(goal) | {_phrase(mention) for mention in (mentions or [])}
    for entry in load():
        by_alias = any(_alias_hit(goal, alias) for alias in [entry.name, *entry.aliases])
        by_domain = any(same_site(domain, known) or same_site(known, domain)
                        for domain in domains for known in entry.domains)
        by_name = any(_phrase(alias) in topics for alias in entry.aliases if len(_phrase(alias)) >= 5)
        if (by_alias or by_domain or by_name) and entry not in found:
            found.append(entry)
    return found


def by_id(identifier: str) -> SourceEntry | None:
    return next((entry for entry in load() if entry.id == identifier), None)


def for_url(url: str, ids: list[str]) -> SourceEntry | None:
    """The registry entry among ``ids`` whose site ``url`` is on."""
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").removeprefix("www.")
    for identifier in ids:
        entry = by_id(identifier)
        if entry and any(same_site(host, domain) for domain in entry.domains):
            return entry
    return None
