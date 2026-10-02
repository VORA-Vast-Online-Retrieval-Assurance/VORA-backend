"""Reading numbers written for people: "$54,669", "Rs. 14.49 Lakh", "down 20%"."""

from __future__ import annotations

import re

NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_SCALES = (
    (re.compile(r"^\s*(?:crore|cr)\b"), 1e7), (re.compile(r"^\s*(?:lakh|lac)\b"), 1e5),
    (re.compile(r"^\s*(?:trillion|tn)\b"), 1e12), (re.compile(r"^\s*(?:billion|bn)\b"), 1e9),
    (re.compile(r"^\s*(?:million|mn)\b"), 1e6), (re.compile(r"^\s*(?:thousand|k)\b"), 1e3),
)
CURRENCY = re.compile(r"[$€£¥₹₩₽]|\b(?:usd|eur|gbp|inr|jpy|cny|rs\.?)\b", re.I)


def to_number(value: object) -> float | None:
    """"$54,669", "Rs. 14.49 Lakh", "down 20%" -> a number (same rules as the UI)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("−", "-").replace("–", "-")
    match = NUMBER.search(text)
    if not match:
        return None
    try:
        number = float(match.group(0).replace(",", ""))
    except ValueError:
        return None
    tail = text[match.end():].casefold()
    for pattern, factor in _SCALES:
        if pattern.search(tail):
            number *= factor
            break
    if re.search(r"\bdown\b|\bdecrease|\bfell\b", text.casefold()) and number > 0:
        number = -number
    return number
