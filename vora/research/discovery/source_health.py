"""Which sites are worth visiting: every candidate site gets one cheap request first, and a site that errors is
blacklisted for everyone.

* ``vet`` runs when a batch receives its list of sites: blacklisted hosts are dropped without a request, the rest are
  probed in parallel (a host that answered recently is not probed again), and a host that cannot be reached is added
  to the universal blacklist (``blacklisted_sources`` in the database: a fact about a public host, no owner, no user
  data), so no other track spends time on it.
* ``export_files`` writes the two data files from the database every ``VORA_SYNC_MINUTES``:
  ``data/blacklistedSources.json`` (the blacklist) and ``data/sources.json`` (sites whose listing was learned and
  has read cleanly, as registry entries marked ``auto``; hand-written entries are kept as they are).
* ``import_blacklist`` reads ``data/blacklistedSources.json`` back on start, so a host added to the file by hand
  is honoured too.

An error means the site cannot be reached at all (name does not resolve, connection refused or timed out, TLS or
protocol failure, not a public address) or the home page is gone (404/410). A site that answers with 403, 5xx or a
slow body is alive: sites that refuse scripts or have a bad hour are not banned for good.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from vora.settings import settings
from vora.shared.urls import safe_get

logger = logging.getLogger("vora.health")

PROBE_TIMEOUT = 12
ALIVE_SECONDS = 1800
_alive: dict[str, float] = {}
_lock = threading.Lock()


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def probe(host: str) -> str | None:
    """None when ``host`` answers; otherwise the reason it cannot be used."""
    last = "unreachable"
    for scheme in ("https", "http"):
        try:
            _, status, _ = safe_get(f"{scheme}://{host}/", timeout=PROBE_TIMEOUT, max_bytes=1024)
        except ValueError:
            return "not a public address"
        except httpx.ReadTimeout:
            return None                                      # it accepted the connection: slow, not dead
        except httpx.HTTPError as exc:
            last = type(exc).__name__
            continue
        except OSError as exc:
            last = type(exc).__name__
            continue
        if status in (404, 410):
            return f"HTTP {status}"
        return None
    return last


def vet(repository, results: list, notes: list[str] | None = None,
        probe_fn: Callable[[str], str | None] = probe) -> list:
    """The candidates whose site is not blacklisted and answers; failures are blacklisted for everyone."""
    if os.getenv("VORA_PROBE_SOURCES", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        return results
    banned = repository.blacklisted_hosts()
    now = time.monotonic()
    hosts = list(dict.fromkeys(_host(item.url) for item in results))
    with _lock:
        fresh = {host for host in hosts if _alive.get(host, 0) > now}
    todo = [host for host in hosts if host and host not in banned and host not in fresh]
    failed: dict[str, str] = {}
    if todo:
        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
            for host, reason in zip(todo, pool.map(probe_fn, todo)):
                if reason is None:
                    with _lock:
                        _alive[host] = now + ALIVE_SECONDS
                else:
                    failed[host] = reason
                    repository.blacklist(host, reason)
    dropped = {host: banned_reason for host, banned_reason in
               {**{h: "blacklisted" for h in hosts if h in banned}, **failed}.items()}
    if dropped and notes is not None:
        notes.append("not visited (unreachable): " + ", ".join(sorted(dropped)))
    return [item for item in results if _host(item.url) not in dropped]


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def blacklist_path() -> Path:
    return Path(settings.source_registry_path).with_name("blacklistedSources.json")


_seen_stamp = 0


def _stamp(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def import_blacklist(repository, only_if_changed: bool = False) -> int:
    """Hosts listed in the blacklist file (added by hand or by another install) join the database. With
    ``only_if_changed`` the file is read only when it changed since this process last wrote or read it, so the
    scheduler can call this every few seconds and a hand edit is live without a restart."""
    global _seen_stamp
    path = blacklist_path()
    stamp = _stamp(path)
    if only_if_changed and stamp == _seen_stamp:
        return 0
    _seen_stamp = stamp
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    count = 0
    for item in payload.get("blacklisted", []) if isinstance(payload, dict) else []:
        host = str(item.get("host", "")).strip().lower() if isinstance(item, dict) else ""
        if host and "/" not in host and len(host) < 254:
            repository.blacklist(host, str(item.get("reason", "listed in the file"))[:200])
            count += 1
    return count


def export_files(repository) -> None:
    """Write blacklistedSources.json and sources.json from the database (atomically; readers never see half a file)."""
    from vora.learning import source_registry

    global _seen_stamp
    _atomic_write(blacklist_path(), {"blacklisted": repository.list_blacklist()})
    _seen_stamp = _stamp(blacklist_path())                  # our own write is not a hand edit
    registry = Path(settings.source_registry_path)
    try:
        existing = json.loads(registry.read_text(encoding="utf-8")).get("sources", [])
    except (OSError, ValueError):
        existing = []
    kept = [entry for entry in existing if isinstance(entry, dict) and not entry.get("auto")]
    taken = {domain for entry in kept for domain in entry.get("domains", [])}
    banned = repository.blacklisted_hosts()
    learned = [{"id": "auto-" + re.sub(r"[^a-z0-9.]+", "-", row["host"]).strip("-"), "name": row["host"],
                "aliases": [row["host"]], "domains": [_host(row["url"])], "entry": row["url"], "auto": True,
                "recipe": row["recipe"]}
               for row in repository.list_learned()
               if row["url"].startswith("https://")             # the registry only takes https entries
               and _host(row["url"]) not in taken and _host(row["url"]) not in banned]
    _atomic_write(registry, {"sources": kept + learned})
    source_registry.load.cache_clear()


def sync(repository) -> None:
    try:
        export_files(repository)
    except Exception:  # noqa: BLE001 - files are a convenience; the database is the record
        logger.exception("Could not update the source files")
