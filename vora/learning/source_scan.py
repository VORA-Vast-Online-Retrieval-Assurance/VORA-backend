"""A scan of the HTML a server sent, for what a rendered page's visible controls do not show.

The rendered page lists only what is visible: a collapsed menu, an unopened tab, an icon-only button or a link in a
hidden panel is missing from it, but it is in the markup. This module reads the markup (never runs it) and returns
candidate controls: links, buttons and inputs with their label, id, address and click handler. A candidate is only a
proposal: the learner finds it on the live page, activates it, and keeps what follows only if the result replays
(see ``vora.learning.structure``). The text of a page is data here, never an instruction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup

MAX_CONTROLS = 400
MAX_LABEL = 80
_HIDDEN_STYLE = re.compile(r"(?i)display\s*:\s*none|visibility\s*:\s*hidden")
_HIDDEN_CLASS = re.compile(r"(?i)\b(hidden|hide|collapse|collapsed|d-none|sr-only|invisible)\b")


@dataclass(slots=True)
class Control:
    kind: str                 # a | button | input
    label: str
    id: str = ""
    name: str = ""
    href: str = ""
    onclick: str = ""
    hidden: bool = False      # hidden by markup or style (a hint: the live page decides)
    in_form: bool = False

    @property
    def selector(self) -> str | None:
        """A selector that finds this control on the live page, from its own id, name or address."""
        for attribute, value in (("id", self.id), ("name", self.name)):
            if value and '"' not in value and "\\" not in value:
                return f'[{attribute}="{value}"]'
        if self.href and self.kind == "a" and '"' not in self.href and not self.href.startswith("#"):
            return f'a[href="{self.href}"]'
        return None


def _label(tag) -> str:
    text = " ".join(tag.get_text(" ", strip=True).split())
    for attribute in ("value", "aria-label", "title", "alt"):
        if not text and tag.get(attribute):
            text = str(tag.get(attribute))
    if not text:
        image = tag.find("img")
        if image is not None:
            text = str(image.get("alt") or image.get("title") or "")
    return text[:MAX_LABEL]


def _hidden(tag) -> bool:
    for node in [tag, *tag.parents]:
        if getattr(node, "attrs", None) is None:
            continue
        if node.has_attr("hidden") or node.get("aria-hidden") == "true":
            return True
        if _HIDDEN_STYLE.search(str(node.get("style", ""))) or _HIDDEN_CLASS.search(" ".join(node.get("class", []))):
            return True
    return False


def scan(html: str, base_url: str = "") -> list[Control]:
    """Candidate controls found in ``html``, in document order (at most ``MAX_CONTROLS``)."""
    soup = BeautifulSoup(html or "", "html.parser")
    found: list[Control] = []
    for tag in soup.find_all(["a", "button", "input"]):
        if len(found) >= MAX_CONTROLS:
            break
        kind = tag.name
        if kind == "input" and str(tag.get("type", "")).lower() not in {"button", "submit", "image"}:
            continue
        href = str(tag.get("href", "")).strip()
        if kind == "a" and not (href or tag.get("onclick")):
            continue
        if href and not href.lower().startswith(("javascript:", "#", "mailto:", "tel:")) and base_url:
            href = urljoin(base_url, href) if False else href       # kept as written: it must match the live attribute
        found.append(Control(kind=kind, label=_label(tag), id=str(tag.get("id", "")), name=str(tag.get("name", "")),
                             href=href, onclick=str(tag.get("onclick", ""))[:200], hidden=_hidden(tag),
                             in_form=tag.find_parent("form") is not None))
    return found
