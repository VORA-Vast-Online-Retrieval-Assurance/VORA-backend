"""CarDekho listing cards (ported from vora/extractors/sites/cardekho.py).

The vora version ran JavaScript in the live page; this port reads the same
structures from the rendered HTML that the engine already captured.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

PRICE = re.compile(r"(?:₹|Rs\.?)\s*[\d,.]+(?:\s*[-–]\s*[\d,.]+)?\s*(?:Lakh|Lac|Cr|Crore)?"
                   r"|[\d.]+\s*[-–]\s*[\d.]+\s*(?:Lakh|Lac|Cr|Crore)|[\d.]+\s*(?:Lakh|Lac|Cr|Crore)", re.I)
RANGE = re.compile(r"\b\d{2,4}\s*km\b", re.I)


def _text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def extract(soup: BeautifulSoup, url: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def push(model: str, price: str, brand: str = "", range_km: str = "") -> None:
        model, price = " ".join(model.split()), " ".join(price.split())
        if len(model) < 2 or not re.search(r"[A-Za-z]", model) or len(model) > 90:
            return
        if not price and not range_km:
            return
        key = (model.casefold(), price)
        if key in seen:
            return
        seen.add(key)
        row = {"model": model, "price": price}
        if brand:
            row["brand"] = " ".join(brand.split())
        if range_km:
            row["range"] = range_km
        rows.append(row)

    # Model cards.
    for card in soup.select("li[data-price], li.gsc_col-xs-12, div[data-model]"):
        title = card.select_one("h3, h2, [class*='title'], a[title]")
        model = (title.get("title") if title and title.get("title") else _text(title))
        price_node = card.select_one("[class*='price' i], .price, span[data-price]")
        price = _text(price_node)
        if not PRICE.search(price):
            match = PRICE.search(_text(card))
            price = match.group(0) if match else ""
        brand = _text(card.select_one("[class*='brand' i], .brandName"))
        range_match = RANGE.search(_text(card.select_one("[class*='range' i]")) or "")
        push(model, price, brand, range_match.group(0) if range_match else "")

    # Links to model pages with a price nearby.
    for anchor in soup.select('a[href*="/car/"], a[href*="/carmodels/"]'):
        card = anchor.find_parent(["li", "article"]) or anchor.find_parent(
            "div", class_=re.compile("card|car", re.I)) or anchor.parent
        if card is None:
            continue
        match = PRICE.search(_text(card))
        push(anchor.get("title") or _text(anchor), match.group(0) if match else "")
    return rows
