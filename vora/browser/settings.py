"""Validated configuration for the foundational engine."""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Measured on three image-heavy pages: peak ~410 MB instead of ~530 MB, and
# slightly faster. Pages render the same (one page is open at a time).
LEAN_ARGUMENTS = (
    "--renderer-process-limit=1",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-features=Translate,OptimizationHints,MediaRouter,BackForwardCache",
    "--disable-gpu",
    "--js-flags=--max-old-space-size=512",
)


@dataclass(frozen=True, slots=True)
class EngineSettings:
    binary_path: Path
    headless: bool = True
    navigation_timeout_ms: int = 30_000
    network_idle_timeout_ms: int = 5_000
    max_network_records: int = 500
    fingerprint_seed: int | None = None
    fingerprint_platform: str | None = None
    # Skip images, video, audio and fonts: extraction never uses them, and they
    # are most of a page's memory and bandwidth.
    block_media: bool = True
    # Fewer background services and renderer processes: about 20% less memory
    # per browser with no effect on what pages show.
    lean: bool = True
    # Speak HTTP/1.1 only. Some servers (older ASP.NET behind proxies, common on government portals)
    # break form postbacks over HTTP/2 with ERR_HTTP2_PROTOCOL_ERROR; HTTP/1.1 works everywhere.
    http1: bool = True
    # Refuse every browser request to a private, local or non-web address (also after redirects, and for the
    # requests a page makes itself). Off only for tests that serve pages from this machine.
    guard_network: bool = True

    @classmethod
    def from_env(cls) -> "EngineSettings":
        raw_path = os.getenv("VORA_BROWSER_BINARY", "").strip()
        if not raw_path:
            raise ValueError(
                "VORA_BROWSER_BINARY is not set: put the absolute path of a Chrome or Chromium "
                "executable in .env, e.g. VORA_BROWSER_BINARY=C:/Program Files/Google/Chrome/Application/chrome.exe"
            )
        seed = os.getenv("VORA_FINGERPRINT_SEED", "").strip()
        return cls(
            binary_path=Path(raw_path).expanduser(),
            headless=_as_bool(os.getenv("VORA_HEADLESS", "true")),
            navigation_timeout_ms=int(os.getenv("VORA_NAVIGATION_TIMEOUT_MS", "30000")),
            network_idle_timeout_ms=int(os.getenv("VORA_NETWORK_IDLE_TIMEOUT_MS", "5000")),
            max_network_records=int(os.getenv("VORA_MAX_NETWORK_RECORDS", "500")),
            fingerprint_seed=int(seed) if seed else None,
            fingerprint_platform=os.getenv("VORA_FINGERPRINT_PLATFORM") or None,
            block_media=_as_bool(os.getenv("VORA_BLOCK_MEDIA", "true")),
            lean=_as_bool(os.getenv("VORA_LEAN_BROWSER", "true")),
            http1=_as_bool(os.getenv("VORA_HTTP1", "true")),
            guard_network=_as_bool(os.getenv("VORA_NETWORK_GUARD", "true")),
        )

    def validate(self) -> "EngineSettings":
        resolved = self.binary_path.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(
                f"Browser binary does not exist: {resolved} (VORA_BROWSER_BINARY must be the executable file, "
                "not its folder)")
        if self.navigation_timeout_ms <= 0 or self.network_idle_timeout_ms <= 0:
            raise ValueError("Engine timeouts must be positive")
        if self.max_network_records < 0:
            raise ValueError("max_network_records cannot be negative")
        return self

    def browser_arguments(self) -> list[str]:
        import secrets

        seed = self.fingerprint_seed or secrets.randbelow(90_000) + 10_000
        profile = self.fingerprint_platform
        if not profile:
            profile = "macos" if platform.system() == "Darwin" else "windows"
        return [
            "--no-sandbox",
            f"--fingerprint={seed}",
            f"--fingerprint-platform={profile}",
            *(LEAN_ARGUMENTS if self.lean else ()),
            *(("--disable-http2",) if self.http1 else ()),
        ]

