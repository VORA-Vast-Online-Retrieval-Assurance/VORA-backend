"""Environment-backed application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class AppSettings:
    database_path: Path = Path(os.getenv("DATABASE_PATH", "./vora.db"))
    host: str = os.getenv("VORA_HOST", "127.0.0.1")
    port: int = int(os.getenv("VORA_PORT", "8000"))
    api_key: str | None = os.getenv("VORA_API_KEY") or None
    # How callers prove who they are: "supabase" (a signed-in user's Supabase access token),
    # "api_key" (one shared key, cookie or X-API-Key) or "none". Empty picks api_key when
    # VORA_API_KEY is set, else none.
    auth: str = os.getenv("VORA_AUTH", "").strip().lower()
    # The interactive API pages (/docs, /redoc, /openapi.json) list every endpoint. They are on while nobody has to
    # sign in (local use) and off otherwise; VORA_DOCS=true or false overrides that.
    docs: str = os.getenv("VORA_DOCS", "").strip().lower()
    supabase_url: str = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    supabase_jwt_audience: str = os.getenv("SUPABASE_JWT_AUDIENCE", "authenticated")
    # Only for projects that still sign tokens with a shared secret (HS256).
    supabase_jwt_secret: str | None = os.getenv("SUPABASE_JWT_SECRET") or None
    # Web origins allowed to call the API from a browser (comma separated).
    cors_origins: tuple[str, ...] = tuple(
        item.strip() for item in os.getenv("VORA_CORS_ORIGINS", "").split(",") if item.strip())
    # Tracks created before sign-in existed belong to this Supabase user id.
    legacy_owner: str | None = os.getenv("VORA_LEGACY_OWNER") or None
    max_candidates: int = int(os.getenv("VORA_MAX_CANDIDATES", "40"))
    max_sources: int = int(os.getenv("VORA_MAX_SOURCES", "8"))
    llm_model: str = os.getenv("LLM_MODEL", "groq/qwen/qwen3.8-27b")
    llm_fallback_model: str | None = os.getenv("LLM_FALLBACK_MODEL") or None
    gemini_api_key: str | None = os.getenv("GEMINI_API_KEY") or None
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    llm_timeout_seconds: int = int(os.getenv("LLM_TIMEOUT_SECONDS", "45"))
    # After a model fails, skip it for this long and use the fallback directly.
    llm_cooldown_seconds: int = int(os.getenv("LLM_COOLDOWN_SECONDS", "600"))
    # Planning waits at most this long for the model, then plans deterministically.
    llm_plan_seconds: float = float(os.getenv("VORA_LLM_PLAN_SECONDS", "20"))
    # Ask the model about table headers the deterministic lexicon cannot place.
    semantic_llm: bool = _flag("VORA_SEMANTIC_LLM", "true")
    # Live tracks start a batch this many seconds after the previous batch started.
    live_interval_seconds: int = int(os.getenv("VORA_LIVE_INTERVAL_SECONDS", "300"))
    # Work budget of one batch; pages not reached are queued for the next batch.
    batch_seconds: int = int(os.getenv("VORA_BATCH_SECONDS", "300"))
    # Batches reuse search results this young while unread candidates remain.
    search_cache_minutes: int = int(os.getenv("VORA_SEARCH_CACHE_MINUTES", "60"))
    # Browser searches are rationed: an engine that blocked us rests (doubling on
    # repeat blocks), searches are spaced out, and an hourly cap applies to all tracks.
    search_cooldown_minutes: float = float(os.getenv("VORA_SEARCH_COOLDOWN_MINUTES", "15"))
    search_interval_seconds: float = float(os.getenv("VORA_SEARCH_INTERVAL_SECONDS", "5"))
    max_searches_per_hour: int = int(os.getenv("VORA_MAX_SEARCHES_PER_HOUR", "30"))
    # Optional official search API (brave | google); used before browser searches.
    search_api: str | None = os.getenv("VORA_SEARCH_API") or None
    search_api_key: str | None = os.getenv("VORA_SEARCH_API_KEY") or None
    search_api_cx: str | None = os.getenv("VORA_SEARCH_API_CX") or None
    # Which search sources are tried, in this order, until one answers: duckduckgo and bing (browser),
    # google and brave (their APIs, when keyed), searxng (when its address is set).
    search_order: tuple[str, ...] = tuple(
        name.strip().casefold() for name in (os.getenv("VORA_SEARCH_ORDER") or "duckduckgo,bing,google,brave,searxng").split(",")
        if name.strip())
    # A self-hosted SearXNG instance (its JSON format enabled), e.g. http://localhost:8080: metasearch with no per-engine
    # blocking. Used when no keyed API is configured.
    searxng_url: str | None = (os.getenv("VORA_SEARXNG_URL") or "").strip().rstrip("/") or None
    # Machine-readable datasets (CSV) downloaded per run from links on rendered pages.
    max_linked_datasets: int = int(os.getenv("VORA_MAX_LINKED_DATASETS", "2"))
    # Source ranking, rotation and merging.
    max_per_domain: int = int(os.getenv("VORA_MAX_PER_DOMAIN", "1"))
    block_cooldown_days: int = int(os.getenv("VORA_BLOCK_COOLDOWN_DAYS", "7"))
    # A page that gave useful rows is re-read only after this many hours.
    revisit_hours: float = float(os.getenv("VORA_REVISIT_HOURS", "24"))
    merge_runs: bool = _flag("VORA_MERGE_RUNS", "true")
    site_adapters: bool = _flag("VORA_SITE_ADAPTERS", "true")
    max_raw_observations: int = int(os.getenv("VORA_MAX_RAW_OBSERVATIONS", "5000"))
    # Interactive pass (tabs, "load more", iframes, chart data) run beside the fast pass.
    deep_lane: bool = _flag("VORA_DEEP_LANE", "true")
    # The interactive pass needs a second browser; skip it when less memory is free.
    deep_lane_min_free_mb: int = int(os.getenv("VORA_DEEP_LANE_MIN_FREE_MB", "1500"))
    # Batches (tracks) that may work at once, and the browsers they share. A batch borrows
    # page-sized turns from the pool, so many batches can be active with few browsers.
    max_concurrent_batches: int = int(os.getenv("VORA_MAX_CONCURRENT_BATCHES", "4"))
    browser_pool_size: int = int(os.getenv("VORA_BROWSER_POOL", "2"))
    browser_recycle_after: int = int(os.getenv("VORA_BROWSER_RECYCLE_AFTER", "200"))
    # How long finished-run history is kept (days); see Repository.purge.
    retention_event_days: int = int(os.getenv("VORA_RETENTION_EVENT_DAYS", "30"))
    retention_run_days: int = int(os.getenv("VORA_RETENTION_RUN_DAYS", "90"))
    retention_history_days: int = int(os.getenv("VORA_RETENTION_HISTORY_DAYS", "180"))
    # Let the interactive pass fill and submit query (search/filter) forms from the request.
    # Login, payment, contact, upload, subscribe and CAPTCHA forms are never touched.
    explore_forms: bool = _flag("VORA_FORMS", "true")
    # Same-site pages the deep pass may follow from each page (one level deep).
    crawl_per_site: int = int(os.getenv("VORA_CRAWL_PER_SITE", "3"))
    # Demote rows from page blocks that do not support the requested data.
    block_support: bool = _flag("VORA_BLOCK_SUPPORT", "true")
    # Shared site knowledge (committed); see vora/research/discovery/knowledge.py.
    site_knowledge_path: str = os.getenv("VORA_SITE_KNOWLEDGE", "data/site_knowledge.json")
    # Official sources read directly when a request names them (see vora/learning/source_registry.py).
    source_registry_path: str = os.getenv("VORA_SOURCE_REGISTRY", "data/sources.json")
    # Pages of a recipe-read listing on a track's first run; later runs stop at known records.
    recipe_max_pages: int = int(os.getenv("VORA_RECIPE_MAX_PAGES", "5"))
    # A data-service listing costs one request per page (no browser), so a first run reads further.
    api_max_pages: int = int(os.getenv("VORA_API_MAX_PAGES", "30"))
    # Official sites for a request that names none: suggested by models, verified by the program
    # (vora/research/planning/source_resolver.py). Off without a key. Their listings are learned, not hardcoded (vora/learning/structure.py).
    groq_api_key: str | None = os.getenv("GROQ_API_KEY") or None
    resolver_models: tuple[str, ...] = tuple(
        m.strip() for m in os.getenv(
            "VORA_RESOLVER_MODELS",
            "groq:qwen/qwen3.8-27b,gemini:gemini-3.5-flash-lite").split(",") if m.strip())
    resolver_sites: int = int(os.getenv("VORA_RESOLVER_SITES", "5"))
    # A learned structure is kept this long, and dropped after this many failed reads in a row.
    # The blacklist and registry data files are rewritten from the database this often.
    sync_minutes: int = int(os.getenv("VORA_SYNC_MINUTES", "30"))
    # Shared reads of public sources (research/reading/source_cache.py): one read serves every track while fresh.
    source_cache: bool = _flag("VORA_SOURCE_CACHE", "true")
    # How long a shared read stays fresh: a listing (a learned recipe's table or list) and an ordinary page.
    source_ttl_listing_minutes: int = int(os.getenv("VORA_SOURCE_TTL_LISTING_MINUTES", "30"))
    source_ttl_page_minutes: int = int(os.getenv("VORA_SOURCE_TTL_PAGE_MINUTES", "180"))
    # Shared answers for the same request: the plan, its official sites and its search results.
    query_cache_minutes: int = int(os.getenv("VORA_QUERY_CACHE_MINUTES", "720"))
    learned_ttl_days: int = int(os.getenv("VORA_LEARNED_TTL_DAYS", "14"))
    learned_max_failures: int = int(os.getenv("VORA_LEARNED_MAX_FAILURES", "2"))

    @property
    def auth_mode(self) -> str:
        if self.auth in {"supabase", "api_key", "none"}:
            return self.auth
        return "api_key" if self.api_key else "none"

    @property
    def docs_enabled(self) -> bool:
        if self.docs in {"1", "true", "yes", "on"}:
            return True
        if self.docs in {"0", "false", "no", "off"}:
            return False
        return self.auth_mode == "none"

    def resolved_database_path(self) -> Path:
        return self.database_path if self.database_path.is_absolute() else ROOT / self.database_path


settings = AppSettings()

