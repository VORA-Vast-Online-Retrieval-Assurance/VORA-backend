"""Optional search through an official web-search API.

Search engines treat automated browser searches as bots: they answer with a
challenge page or with results unrelated to the query, most often for a stock
(headless) browser. An API key avoids that entirely. Supported providers:

* ``brave``: Brave Search API (``VORA_SEARCH_API_KEY``)
* ``google``: Google Programmable Search JSON API (``VORA_SEARCH_API_KEY`` and
  ``VORA_SEARCH_API_CX``, the search engine id)
* ``searxng``: a self-hosted SearXNG instance (``VORA_SEARXNG_URL``; its settings must allow ``format=json``).
  It queries many engines itself, so no single engine's bot checks stop discovery, and ``site:`` queries work.

A keyed provider is used when configured, else SearXNG when its address is set. When none is configured, discovery
uses the browser (DuckDuckGo, Bing).
"""

from __future__ import annotations

import httpx

from vora.settings import settings

TIMEOUT_SECONDS = 15.0
PROVIDERS = {"brave", "google", "searxng"}


class SearchApiError(RuntimeError):
    pass


def configured() -> str | None:
    provider = (settings.search_api or "").strip().casefold()
    if provider in {"brave", "google"} and settings.search_api_key and (provider != "google" or settings.search_api_cx):
        return provider
    if settings.searxng_url and provider in {"", "searxng"}:
        return "searxng"
    return None


def available() -> set[str]:
    """The API sources that are set up: Google or Brave with their key, SearXNG with its address."""
    provider = (settings.search_api or "").strip().casefold()
    found = set()
    if provider in {"brave", "google"} and settings.search_api_key and (provider != "google" or settings.search_api_cx):
        found.add(provider)
    if settings.searxng_url:
        found.add("searxng")
    return found


def api_search(query: str, region: tuple[str, str] | None = None,
               transport: httpx.BaseTransport | None = None, provider: str | None = None) -> list[tuple[str, str, str]]:
    """(url, title, snippet) results for ``query`` from ``provider`` (default: the configured one)."""
    provider = provider or configured()
    if provider is None:
        raise SearchApiError("No search API configured")
    with httpx.Client(timeout=TIMEOUT_SECONDS, transport=transport) as client:
        if provider == "searxng":
            params = {"q": query, "format": "json", "safesearch": 0}
            if region:
                params["language"] = f"{region[1]}-{region[0]}" if len(region) > 1 and region[1] else region[0].lower()
            response = client.get(f"{settings.searxng_url}/search", params=params,
                                  headers={"Accept": "application/json", "User-Agent": "VORA"})
            response.raise_for_status()
            items = response.json().get("results", [])
            return [(item.get("url", ""), item.get("title", ""), item.get("content", "")) for item in items
                    if str(item.get("url", "")).startswith(("http://", "https://"))][:20]
        if provider == "brave":
            params = {"q": query, "count": 20}
            if region:
                params["country"] = region[0].lower()
            response = client.get("https://api.search.brave.com/res/v1/web/search", params=params,
                                  headers={"Accept": "application/json",
                                           "X-Subscription-Token": settings.search_api_key or ""})
            response.raise_for_status()
            items = response.json().get("web", {}).get("results", [])
            return [(item.get("url", ""), item.get("title", ""), item.get("description", "")) for item in items]
        params = {"key": settings.search_api_key, "cx": settings.search_api_cx, "q": query, "num": 10}
        if region:
            params["gl"] = region[0].lower()
        response = client.get("https://www.googleapis.com/customsearch/v1", params=params)
        response.raise_for_status()
        items = response.json().get("items", [])
        return [(item.get("link", ""), item.get("title", ""), item.get("snippet", "")) for item in items]
