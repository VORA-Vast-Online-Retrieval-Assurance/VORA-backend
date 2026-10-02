"""Headless browser execution with event-driven integration points."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from vora.browser.contracts import ExecutionResult, NetworkRecord
from vora.browser.events import EventBus, LifecycleEvent, RuntimeEvent
from vora.browser.explore import Exploration, ExploreHints, explore
from vora.browser.settings import EngineSettings

if TYPE_CHECKING:       # the learner and recipe runner import the engine, so these are only for type hints
    from vora.learning.recipes import Recipe, RecipeRun
    from vora.learning.structure import LearnedStructure


class EngineNotStartedError(RuntimeError):
    pass


# Requests for images, video, audio and fonts, recognised by their file name.
# Matching by URL keeps every other request off the Python side.
MEDIA_REQUEST = re.compile(
    r"\.(?:png|jpe?g|gif|webp|avif|bmp|ico|svg|mp4|webm|m4v|mov|mp3|m4a|ogg|wav|woff2?|ttf|otf|eot)(?:[?#]|$)",
    re.I,
)


# Schemes a page may use for its own content; everything else (file:, ftp:, chrome:, ...) is refused.
_PAGE_SCHEMES = ("http:", "https:", "data:", "blob:", "about:")


def guard_request(route: Any) -> None:
    """Let a request through only if it goes to the public internet (or is the page's own inline content)."""
    from urllib.parse import urlparse

    from vora.shared.urls import host_is_public

    url = route.request.url
    if url.startswith(("data:", "blob:", "about:")):
        return route.continue_()
    parts = urlparse(url)
    if parts.scheme in ("http", "https") and host_is_public(parts.hostname or ""):
        return route.continue_()
    return route.abort("blockedbyclient")


def new_context(engine: "BrowserEngine", *, downloads: bool = False) -> Any:
    """An isolated browser context: no network access to private addresses, media dropped when configured."""
    context = engine._browser.new_context(accept_downloads=downloads)
    if engine.settings.guard_network:
        # Registered first, so it runs for every request, including each redirect hop and every subresource.
        context.route("**/*", guard_request)
    if engine.settings.block_media:
        context.route(MEDIA_REQUEST, lambda route: route.abort())
    return context


class BrowserEngine(AbstractContextManager["BrowserEngine"]):
    """Own one browser process and execute isolated page contexts."""

    def __init__(self, settings: EngineSettings, events: EventBus | None = None) -> None:
        self.settings = settings
        self.events = events or EventBus()
        self._playwright: Any = None
        self._browser: Any = None

    @property
    def started(self) -> bool:
        return self._browser is not None

    def start(self) -> "BrowserEngine":
        if self.started:
            return self
        self.settings.validate()
        from playwright.sync_api import sync_playwright

        runtime = sync_playwright().start()
        try:
            browser = runtime.chromium.launch(
                executable_path=str(self.settings.binary_path.resolve()),
                headless=self.settings.headless,
                args=self.settings.browser_arguments(),
                ignore_default_args=["--enable-automation", "--enable-unsafe-swiftshader"],
            )
        except Exception:
            runtime.stop()
            raise
        self._playwright = runtime
        self._browser = browser
        return self

    def close(self) -> None:
        browser, runtime = self._browser, self._playwright
        self._browser = None
        self._playwright = None
        try:
            if browser is not None:
                browser.close()
        finally:
            if runtime is not None:
                runtime.stop()

    def __enter__(self) -> "BrowserEngine":
        return self.start()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _emit(self, name: LifecycleEvent, execution_id: str, **data: object) -> None:
        self.events.emit(RuntimeEvent(
            name=name,
            execution_id=execution_id,
            data=MappingProxyType(dict(data)),
        ))

    def explore(self, url: str, *, budget_seconds: float = 30.0,
                should_stop: Callable[[], bool] | None = None, hints: ExploreHints | None = None) -> Exploration:
        """Reveal content behind scrolling, tabs, "load more", pagination and
        iframes; each distinct state is emitted like a completed execution.
        See ``vora.browser.explore``."""
        if not self.started:
            raise EngineNotStartedError("Call start() or use BrowserEngine as a context manager")
        return explore(self, url, budget_seconds=budget_seconds, should_stop=should_stop, hints=hints)

    def learn_structure(self, url: str, **options: Any) -> "LearnedStructure":
        """Work out how to read a site's listing (see ``vora.learning.structure``); the result carries a recipe."""
        if not self.started:
            raise EngineNotStartedError("Call start() or use BrowserEngine as a context manager")
        from vora.learning.structure import learn

        return learn(self, url, **options)

    def run_recipe(self, url: str, recipe: "Recipe", **options: Any) -> "RecipeRun":
        """Read a site's listing with a recipe (``vora.learning.recipes``)."""
        if not self.started:
            raise EngineNotStartedError("Call start() or use BrowserEngine as a context manager")
        from vora.learning.recipes import run_recipe

        return run_recipe(self, url, recipe, **options)

    def execute(self, url: str) -> ExecutionResult:
        if not self.started:
            raise EngineNotStartedError("Call start() or use BrowserEngine as a context manager")

        execution_id = uuid.uuid4().hex
        started_at = time.monotonic()
        network: list[NetworkRecord] = []
        context = None

        def record_request(request: Any) -> None:
            record = NetworkRecord(
                url=request.url,
                method=request.method,
                resource_type=request.resource_type,
            )
            if len(network) < self.settings.max_network_records:
                network.append(record)
            self._emit(LifecycleEvent.NETWORK_REQUEST, execution_id, record=record)

        def record_response(response: Any) -> None:
            headers = response.headers
            record = NetworkRecord(
                url=response.url,
                method=response.request.method,
                resource_type=response.request.resource_type,
                status=response.status,
                content_type=headers.get("content-type", ""),
            )
            if len(network) < self.settings.max_network_records:
                network.append(record)
            self._emit(LifecycleEvent.NETWORK_RESPONSE, execution_id, record=record)

        self._emit(LifecycleEvent.EXECUTION_START, execution_id, url=url)
        try:
            context = new_context(self)
            page = context.new_page()
            page.set_default_timeout(self.settings.navigation_timeout_ms)
            page.on("request", record_request)
            page.on("response", record_response)
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.settings.navigation_timeout_ms,
            )
            idle_reached = True
            try:
                page.wait_for_load_state(
                    "networkidle", timeout=self.settings.network_idle_timeout_ms
                )
            except Exception:
                idle_reached = False
            self._emit(
                LifecycleEvent.NETWORK_IDLE,
                execution_id,
                reached=idle_reached,
                url=page.url,
            )
            result = ExecutionResult(
                execution_id=execution_id,
                requested_url=url,
                final_url=page.url,
                title=page.title(),
                html=page.content(),
                status=response.status if response is not None else None,
                elapsed_seconds=round(time.monotonic() - started_at, 6),
                network_idle_reached=idle_reached,
                network=tuple(network),
                metadata=MappingProxyType({
                    "headless": self.settings.headless,
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "response_headers": MappingProxyType({
                        key: value for key, value in (response.headers.items() if response else [])
                        if key.casefold() in {"date", "last-modified", "etag"}
                    }),
                }),
            )
            self._emit(LifecycleEvent.EXECUTION_COMPLETE, execution_id, result=result)
            return result
        except Exception as exc:
            self._emit(
                LifecycleEvent.EXECUTION_FAILED,
                execution_id,
                url=url,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise
        finally:
            if context is not None:
                context.close()
