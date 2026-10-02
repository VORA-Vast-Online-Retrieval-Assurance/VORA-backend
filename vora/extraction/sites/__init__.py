"""Optional site adapters.

The general extractors are site-independent. An adapter adds rows for one
website whose listings the general extractors read poorly. Adapters run *in
addition to* the general extractors, their rows are scored like any other,
and ``VORA_SITE_ADAPTERS=false`` turns them off.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from vora.shared.urls import same_site

from vora.extraction.sites import cardekho, carwale

Adapter = Callable[[BeautifulSoup, str], list[dict[str, str]]]

ADAPTERS: dict[str, Adapter] = {
    "cardekho.com": cardekho.extract,
    "carwale.com": carwale.extract,
}


def adapter_for(url: str) -> tuple[str, Adapter] | None:
    host = (urlparse(url).hostname or "").removeprefix("www.")
    for domain, adapter in ADAPTERS.items():
        if same_site(host, domain):
            return domain, adapter
    return None
