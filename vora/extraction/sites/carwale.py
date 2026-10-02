"""CarWale listing cards (ported from vora/extractors/sites/carwale.py).

The vora version ran JavaScript in the live page; this port reads the same
structures from the rendered HTML that the engine already captured.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

PRICE = re.compile(r"(?:Rs\.?|₹)\s*[\d,.]+(?:\s*[-–]\s*[\d,.]+)?\s*(?:Lakh|Lac|Cr|Crore)?", re.I)
SKIP = re.compile(r"recommended|priced from|under rs", re.I)


def _text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def extract(soup: BeautifulSoup, url: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def push(model: str, price: str) -> None:
        model, price = " ".join(model.split()), " ".join(price.split())
        if not re.search(r"[A-Za-z]{2}", model) or not re.search(r"\d", price) or len(model) > 90:
            return
        if SKIP.search(model):
            return
        key = (model.casefold(), price)
        if key not in seen:
            seen.add(key)
            rows.append({"model": model, "price": price})

    for name in soup.select('[data-testing-id="model-name"], .o-jc-card-title, [class*="model-name"], h3'):
        card = name.find_parent(attrs={"data-testing-id": "make-model-card"}) or \
            name.find_parent(["li", "article"]) or name.parent
        if card is None:
            continue
        price = _text(card.select_one('[class*="price" i], [data-testing-id*="price"]'))
        if not PRICE.search(price):
            match = PRICE.search(_text(card))
            price = match.group(0) if match else ""
        push(_text(name), price)

    for anchor in soup.select('a[href*="/new/"], a[href*="/electric-cars/"]'):
        href = anchor.get("href", "")
        if not re.search(r"/new/[a-z0-9-]+/", href, re.I) and "cars" not in href:
            continue
        model = _text(anchor)
        if not 3 <= len(model) <= 80:
            continue
        card = anchor.find_parent(["li", "article"]) or anchor.parent
        match = PRICE.search(_text(card)) if card is not None else None
        if match:
            push(model, match.group(0))
    return rows
