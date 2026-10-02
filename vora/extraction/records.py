"""Records that are not numbers: documents, events, companies, products, people.

A row can answer a request without containing any quantity. What makes it an
answer is structure and evidence, not a numeric value:

* it has something that names the record (a title, a name, a subject line);
* it carries further typed attributes (a date, a link, an identifier, another
  descriptive field), so it is a *record* and not a lone link or a label.

Nothing here knows about any topic or website. The field types (URL, date,
identifier, ...) come from the values, and the rules are the same for a
gazette notice, a conference, a company and a software product.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from vora.extraction.files import file_extension
from vora.extraction.noise import UI_WORDS
from vora.extraction.semantics import tokens, value_kind
from vora.extraction.temporal import find_periods

URL = re.compile(r"^(?:https?://|www\.)\S+$", re.I)
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
# Identifier-shaped values ("CG-DL-E-12092026-268341", "RBI/2026-27/41").
IDENTIFIER = re.compile(r"^(?=.*\d)[A-Za-z0-9][A-Za-z0-9._/-]{4,}$")
IDENTIFIER_LABEL = re.compile(r"(?:^|_)(?:id|no|num|number|ref|reference|code|serial|sr|circular|notification)(?:_|$)")
TITLE_LABEL = re.compile(
    r"(?:^|_)(?:title|name|subject|heading|headline|event|product|company|organi[sz]ation|"
    r"institution|notification|circular|topic|tool|program(?:me)?|project)(?:_|$)")
GENERIC_LINK_LABELS = frozenset({
    "download", "view", "pdf", "open", "details", "detail", "read more", "more", "link", "click here",
    "here", "get", "file", "doc", "document", "full text", "website", "visit", "official website",
    "website link", "learn more", "know more", "see details", "view details", "view pdf",
    "download pdf", "attachment",
})
MIN_TITLE_LETTERS = 3
MAX_TITLE_LENGTH = 300
# From this many words a value is a sentence, not a name (titles are shorter).
PROSE_WORDS = 16
# Labels of a database's own bookkeeping: a bare "id", a "slug", an ordering position.
# They identify a row inside one site's system; they say nothing about the thing itself.
INTERNAL_LABEL = re.compile(r"(?:^|_)(?:id|uuid|guid|slug|pk)$|(?:order|position|index|weight)$")
FLAGS = frozenset({"true", "false", "yes", "no", "null", "none", "n/a"})
# Words that belong to a date ("Mar 16-19, 2026", "Q3 FY2025"). A value with two or more other words
# is a name that mentions a year ("GITEX Global 2026"), not a date.
DATE_WORDS = frozenset({
    "jan", "january", "feb", "february", "mar", "march", "apr", "april", "may", "jun", "june", "jul", "july",
    "aug", "august", "sep", "sept", "september", "oct", "october", "nov", "november", "dec", "december",
    "mon", "monday", "tue", "tues", "tuesday", "wed", "wednesday", "thu", "thur", "thurs", "thursday",
    "fri", "friday", "sat", "saturday", "sun", "sunday",
    "to", "and", "from", "until", "till", "through", "thru", "of", "the", "st", "nd", "rd", "th", "at", "on",
    "fy", "q", "h", "quarter", "week", "year", "month", "day", "between", "am", "pm", "utc", "gmt", "ist",
})


def is_generic_link_label(text: str) -> bool:
    return " ".join(text.casefold().split()) in GENERIC_LINK_LABELS


def field_kind(label: str, value: str) -> str:
    """The type of a value: url, email, date, identifier, money, percent, number or text."""
    text = str(value or "").strip()
    if not text:
        return "empty"
    if URL.match(text):
        return "url"
    if EMAIL.match(text):
        return "email"
    kind = value_kind(text)
    if kind == "period":
        return "date"
    if kind in {"money", "percent", "number"}:
        # A bare number under an identifier-like label is an identifier ("Circular No: 41").
        return "identifier" if IDENTIFIER_LABEL.search("_".join(tokens(label))) and kind == "number" else kind
    if " " not in text and IDENTIFIER.match(text):
        segments = [part for part in re.split(r"[-_/]", text) if part]
        if len(segments) >= 3 or IDENTIFIER_LABEL.search("_".join(tokens(label))):
            return "identifier"
    if len(text.split()) <= 8 and find_periods(text):
        others = [word for word in re.findall(r"[^\W\d_]+", text) if word.casefold() not in DATE_WORDS]
        return "text" if len(others) >= 2 else "date"
    return "text"


def is_document_url(value: str) -> bool:
    """A link to a file (PDF, spreadsheet, ...), which marks a row as a document record."""
    return bool(URL.match(value.strip())) and file_extension(value.strip()) is not None


@dataclass(slots=True)
class RecordVerdict:
    ok: bool = False
    partial: bool = False
    coverage: float = 0.0
    title_field: str | None = None
    kinds: dict[str, str] = field(default_factory=dict)
    summary: str = ""


def _letters(text: str) -> int:
    return sum(1 for character in text if character.isalpha())


def _title_candidates(fields: dict[str, str], kinds: dict[str, str]) -> list[str]:
    candidates = []
    for name, value in fields.items():
        text = str(value).strip()
        if kinds.get(name) != "text" or _letters(text) < MIN_TITLE_LETTERS or len(text) > MAX_TITLE_LENGTH:
            continue
        if text.casefold() in UI_WORDS or is_generic_link_label(text):
            continue
        if name == "statement" or len(text.split()) >= PROSE_WORDS:
            continue  # a sentence lifted from running text, not the name of something
        candidates.append(name)
    return candidates


def _informative(title: str, values: dict[str, str], kinds: dict[str, str]) -> dict[str, str]:
    """The attributes that tell something about the record beyond its name.

    A site's own keys ("id: 24", "position"), flags ("enabled: true") and values that only
    repeat the name (a "slug" of "apparel-garments" beside "Apparel & Garments") describe the
    database, not the thing, so a row with nothing else is a lookup entry, not a record.
    """
    name_words = set(tokens(values[title]))
    kept: dict[str, str] = {}
    for name, kind in kinds.items():
        value = values.get(name, "")
        if name == title or kind == "empty" or value.casefold() in FLAGS:
            continue
        if INTERNAL_LABEL.search("_".join(tokens(name))):
            long_id = kind == "identifier" and " " not in value and len([s for s in re.split(r"[-_/]", value) if s]) >= 3
            if not long_id:
                continue
        words = set(tokens(value))
        if words and words <= name_words:
            continue
        kept[name] = kind
    return kept


def assess_record(fields: dict[str, str]) -> RecordVerdict:
    """Whether a row is a well-formed record, judged from its structure alone."""
    values = {name: str(value).strip() for name, value in fields.items()
              if str(value).strip() and name not in {"series"}}
    kinds = {name: field_kind(name, value) for name, value in values.items()}
    candidates = _title_candidates(values, kinds)
    if not candidates:
        return RecordVerdict(kinds=kinds, summary="No field names the record")

    def rank(name: str) -> tuple[int, int, int]:
        hinted = bool(TITLE_LABEL.search("_".join(tokens(name))))
        return (0 if hinted else 1, 0 if len(values[name].split()) >= 2 else 1, -len(values[name]))

    title = min(candidates, key=rank)
    informative = _informative(title, values, kinds)
    hard = {kind for kind in informative.values() if kind in {"date", "identifier", "url", "email"}}
    has_document = any(is_document_url(values[name]) for name in informative)

    words = len(values[title].split())
    hinted_title = bool(TITLE_LABEL.search("_".join(tokens(title))))
    # A short label ("Home", "About Us") with a lone link is navigation, not a record.
    named = words >= 3 or hinted_title or bool(hard - {"url"}) or has_document
    ok = named and len(informative) >= 2
    partial = named and len(informative) >= 1
    coverage = min(1.0, 0.5 + 0.1 * len(informative) + (0.1 if hard else 0.0))
    parts = ", ".join(sorted({kind for kind in informative.values()}))
    summary = f"'{title}' with {len(informative)} attribute(s) ({parts})" if informative else f"'{title}' alone"
    return RecordVerdict(ok=ok, partial=partial, coverage=round(coverage, 3), title_field=title,
                         kinds=kinds, summary=summary)


def natural_key(fields: dict[str, str]) -> str | None:
    """A record's own identifier, when it has one that is safe to merge on.

    Only a value under an identifier-like label ("gazette_id", "circular_no", "notification number")
    that looks like a real reference ("CG-DL-E-30092026-276649", "RBI/2026-27/41") counts: at least six
    characters with a letter and a digit, or three separated parts. A bare row number ("id: 24") never
    does, since two different records on one site could share it.
    """
    for label, value in fields.items():
        text = str(value or "").strip()
        if not IDENTIFIER_LABEL.search("_".join(tokens(label))) or " " in text or not IDENTIFIER.match(text):
            continue
        parts = [part for part in re.split(r"[-_/.]", text) if part]
        mixed = any(c.isalpha() for c in text) and any(c.isdigit() for c in text)
        if (len(text) >= 6 and mixed) or len(parts) >= 3:
            return f"{label}={text.upper()}"
    return None


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")
