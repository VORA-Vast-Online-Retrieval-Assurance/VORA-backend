"""Structural noise detection that runs before any semantic scoring.

Everything here is deterministic and topic-independent: it recognises the
shape of web chrome (navigation, consent banners, bot challenges, metadata
blocks), not the subject of a page.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

from vora.extraction.semantics import has_quantity, value_kind

CHALLENGE_PATTERNS = re.compile(
    r"just a moment|attention required|checking your browser|verify(?:ing)? you are (?:a )?human|"
    r"performing security verification|security check to access|are you a robot|"
    r"enable javascript and cookies to continue|ddos protection by|cf-browser-verification|"
    r"captcha|access denied|request blocked|unusual traffic from your computer|"
    r"press (?:&|and) hold|bot detection|you have been blocked|error 1020|bots use \w+ too|"
    r"confirm (?:this search|that you) (?:was made by|are) a human|select all (?:squares|images) (?:containing|with)",
    re.I,
)
LOGIN_PATTERNS = re.compile(
    r"\b(?:sign in|log in|login) to (?:continue|view|access|read)|subscribe to (?:continue|read)|"
    r"create (?:a free|an) account to|this content is for (?:subscribers|members)",
    re.I,
)
CONSENT_PATTERNS = re.compile(
    r"\b(?:we use cookies|this (?:site|website) uses cookies|cookie (?:policy|settings|preferences)|"
    r"accept all cookies|manage (?:consent|cookies)|your privacy choices)\b",
    re.I,
)

# Class/id fragments that mark page chrome. Matched against hyphen/underscore
# separated parts so that "table-header" or "price-summary" are not affected.
NOISE_PARTS = frozenset({
    "nav", "navbar", "navigation", "menu", "menubar", "breadcrumb", "breadcrumbs", "footer",
    "cookie", "cookies", "consent", "gdpr", "social", "share", "sharing", "newsletter",
    "subscribe", "advert", "advertisement", "ads", "promo", "popup", "modal", "login",
    "signin", "signup", "masthead", "sitemap", "skip", "toolbar", "related", "recommended",
    "recommendations", "offcanvas", "drawer", "megamenu", "topbar", "banner",
})
NOISE_TAGS = ("nav", "footer", "aside", "dialog", "noscript", "template")
NOISE_ROLES = frozenset({"navigation", "banner", "contentinfo", "search", "dialog", "menu", "menubar"})

UI_WORDS = frozenset({
    "search", "close", "menu", "open", "login", "log in", "sign in", "sign up", "subscribe",
    "share", "next", "previous", "prev", "more", "less", "read more", "learn more", "home",
    "back", "skip", "toggle", "download", "print", "email", "×", "✕", "x", "…", "...",
    "loading", "submit", "go", "ok", "cancel", "accept", "reject", "settings",
})

# JSON-LD types that always describe the page, its authorship or navigation
# rather than facts about the world.
METADATA_TYPES = frozenset({
    "website", "webpage", "breadcrumblist", "listitem", "imageobject", "searchaction",
    "entrypoint", "contactpoint", "datacatalog", "datadownload", "sitenavigationelement",
    "article", "newsarticle", "blogposting", "readaction", "collectionpage", "aboutpage",
    "faqpage", "question", "answer", "videoobject", "wpheader", "wpfooter", "wpsidebar",
    "person", "dataset", "creativework", "mediaobject", "postaladdress", "webcontent",
    "scholarlyarticle", "techarticle", "reportagenewsarticle", "profilepage",
})
# Types that are metadata only when they describe the publishing site itself;
# a list of companies or places can be genuine data for some requests.
PUBLISHER_TYPES = frozenset({
    "organization", "corporation", "localbusiness", "automotivebusiness", "brand", "place",
    "governmentorganization", "educationalorganization", "newsmediaorganization",
    "onlinestore", "store", "ngo",
})
METADATA_LABELS = re.compile(
    r"^(?:last[ _-]?updated|updated|last[ _-]?modified|modified|created|published|"
    r"publication[ _-]?date|release[ _-]?date|author|authors|maintainer|contact|license|licence|"
    r"format|version|identifier|doi|issn|isbn|language|publisher|source|citation|keywords|"
    r"tags|frequency|update[ _-]?frequency|coverage|spatial|temporal|status)$",
    re.I,
)
# Labels that are page metadata only when their value is not a quantity:
# "Coverage: 85%" or "Source: 1,200 units" can be data.
SOFT_METADATA_LABELS = frozenset({"source", "coverage", "status"})


def _metadata_label(label: str, value: str) -> bool:
    label = label.strip()
    if not METADATA_LABELS.match(label):
        return False
    return not (label.casefold() in SOFT_METADATA_LABELS and has_quantity(value))
LINK_KEYS = frozenset({"url", "link", "href", "name", "title", "text", "heading", "label", "image",
                       "website", "document"})


@dataclass(frozen=True, slots=True)
class NoiseVerdict:
    role: str            # data | metadata | navigation | challenge | noise
    probability: float   # 0 = clean data, 1 = certainly noise
    reason: str = ""


def is_challenge_page(title: str, text: str) -> bool:
    sample = f"{title} {text[:1500]}"
    return bool(CHALLENGE_PATTERNS.search(sample))


def is_login_wall(text: str) -> bool:
    return bool(LOGIN_PATTERNS.search(text[:3000]))


def is_noise_class(values: object) -> bool:
    if not values:
        return False
    names = values if isinstance(values, (list, tuple)) else str(values).split()
    for name in names:
        parts = re.split(r"[-_\s]+", str(name).casefold())
        if any(part in NOISE_PARTS for part in parts):
            return True
    return False


def jsonld_types(fields: Mapping[str, str]) -> set[str]:
    raw = fields.get("type", "") or fields.get("schema_type", "")
    return {part.strip().rsplit(":", 1)[-1].casefold() for part in raw.split(",") if part.strip()}


def _describes_site(fields: Mapping[str, str]) -> bool:
    reference = fields.get("id") or fields.get("url") or ""
    if "#organization" in reference.casefold() or "#website" in reference.casefold():
        return True
    parsed = urlparse(fields.get("url", ""))
    return bool(parsed.netloc) and parsed.path in {"", "/"}


def _is_ui_text(value: str) -> bool:
    text = value.strip().casefold()
    return text in UI_WORDS or (len(text) <= 2 and not text.isalnum())


def assess(fields: Mapping[str, str], method: str, declared_role: str = "data") -> NoiseVerdict:
    """Classify an observation's structural role from its fields alone.

    Works for freshly parsed rows and for legacy rows that were stored before
    DOM-level filtering existed.
    """
    if declared_role in {"challenge", "navigation", "metadata", "noise"}:
        reasons = {
            "challenge": "Security verification or bot-challenge page",
            "navigation": "Navigation or link list",
            "metadata": "Page metadata rather than data",
            "noise": "Page chrome",
        }
        return NoiseVerdict(declared_role, 1.0 if declared_role != "metadata" else 0.9,
                            reasons[declared_role])
    values = [str(value) for value in fields.values() if str(value).strip()]
    if not values:
        return NoiseVerdict("noise", 1.0, "Empty observation")
    joined = " ".join(values)
    if method == "page_summary" and is_challenge_page(fields.get("title", ""), joined):
        return NoiseVerdict("challenge", 1.0, "Security verification or bot-challenge page")
    if method == "page_summary" and is_login_wall(joined):
        return NoiseVerdict("challenge", 0.95, "Login or subscription wall")
    if CONSENT_PATTERNS.search(joined[:600]) and not any(has_quantity(value) for value in values[:3]):
        return NoiseVerdict("noise", 0.95, "Cookie or consent banner")

    if method == "json_ld":
        types = jsonld_types(fields)
        if types and types <= METADATA_TYPES:
            return NoiseVerdict("metadata", 0.9, f"Structured page metadata ({', '.join(sorted(types))})")
        if types and types <= METADATA_TYPES | PUBLISHER_TYPES and _describes_site(fields):
            return NoiseVerdict("metadata", 0.85, "Publisher or site description")

    keys = set(fields)
    if len(keys) <= 3 and "value" in keys and keys & {"field", "property", "attribute", "key", "name", "label"}:
        label = next((fields[key] for key in ("field", "property", "attribute", "key", "name", "label")
                      if key in fields), "")
        if _metadata_label(label, fields.get("value", "")):
            return NoiseVerdict("metadata", 0.85, f"Page property '{label.strip()}'")
    if keys and all(_metadata_label(key.replace("_", " "), value) for key, value in fields.items()):
        return NoiseVerdict("metadata", 0.85, "Page properties")

    if all(_is_ui_text(value) for value in values):
        return NoiseVerdict("noise", 0.95, "Interface controls")

    quantities = [value for key, value in fields.items()
                  if key not in {"url", "link", "href", "image"} and value_kind(value) in {"money", "percent", "number"}]
    from vora.extraction.files import file_extension

    points_at_file = any(str(value).startswith(("http://", "https://")) and file_extension(str(value))
                         for value in fields.values())
    if keys <= LINK_KEYS and not quantities and not points_at_file:
        text = fields.get("text") or fields.get("name") or fields.get("title") or ""
        if "url" in keys or "link" in keys or len(text.split()) <= 6:
            return NoiseVerdict("navigation", 0.9, "Navigation or link list")
    if method == "page_summary" and not quantities:
        return NoiseVerdict("data", 0.55, "Unstructured page text")
    if not quantities and len(values) <= 2 and all(len(value.split()) <= 3 for value in values):
        return NoiseVerdict("data", 0.45, "Short text without values")
    return NoiseVerdict("data", 0.0, "")
