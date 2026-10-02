"""Local VORA server entry point."""

import uvicorn

from vora.settings import settings


class Server(uvicorn.Server):
    """Closes live-update streams as soon as a stop is requested (Ctrl+C)."""

    def handle_exit(self, sig, frame) -> None:
        from vora.api.application import shutting_down

        shutting_down.set()
        super().handle_exit(sig, frame)


def describe_settings() -> str:
    """The settings that shape a batch, so a leftover .env override is visible."""
    from vora.research.discovery import search_api

    search = search_api.configured() or f"browser (max {settings.max_searches_per_hour}/hour)"
    return (f"Batches: {settings.max_candidates} candidates, {settings.max_sources} pages, "
            f"{settings.batch_seconds}s budget, live every {settings.live_interval_seconds}s | "
            f"interactive pass {'on' if settings.deep_lane else 'off'} | search: {search}")


def main() -> None:
    print(f"VORA running at http://{settings.host}:{settings.port}")
    print(describe_settings())
    config = uvicorn.Config("vora.api.application:app", host=settings.host, port=settings.port, reload=False,
                            timeout_graceful_shutdown=5)
    try:
        Server(config).run()
    except KeyboardInterrupt:
        # uvicorn re-raises Ctrl+C after its graceful shutdown; the stop is expected.
        pass
    print("VORA stopped")


if __name__ == "__main__":
    main()
