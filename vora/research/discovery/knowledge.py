"""What earlier research learned about websites, shareable through the repository.

Each installation learns from its own runs which sites give data and which
block automated reading (``source_history``). ``scripts/export_site_knowledge.py``
writes that to ``data/site_knowledge.json``, which is committed, so a fresh clone
ranks sources with everyone's experience instead of starting from nothing.

The file keeps one section per installation: exporting again replaces your own
section (never double-counts it), and at runtime an installation combines its
own history with the *other* installations' sections. Only per-site counts are
exported: no goals, page addresses, conversations or data.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vora.settings import settings
from vora.settings import ROOT

from vora.storage.repository import Repository

FIELDS = ("reads", "useful_reads", "accepted_rows", "blocked_reads")
Counts = dict[str, dict[str, int]]


def knowledge_path() -> Path:
    path = Path(settings.site_knowledge_path)
    return path if path.is_absolute() else ROOT / path


def _read(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _sites(section: dict[str, Any]) -> Counts:
    return {str(site["domain"]).casefold(): {field: int(site.get(field, 0)) for field in FIELDS}
            for site in section.get("sites", []) if isinstance(site, dict) and site.get("domain")}


def merge(*sources: Counts) -> Counts:
    """Add up per-site counts from several sources."""
    merged: Counts = {}
    for source in sources:
        for domain, counts in source.items():
            target = merged.setdefault(domain, dict.fromkeys(FIELDS, 0))
            for field in FIELDS:
                target[field] += int(counts.get(field, 0))
    return merged


def shared_knowledge(exclude: str | None = None, path: Path | None = None) -> Counts:
    """Counts from the committed file, leaving out one installation's own section."""
    contributions = _read(path or knowledge_path()).get("contributions", {})
    return merge(*(_sites(section) for installation, section in contributions.items()
                   if installation != exclude and isinstance(section, dict)))


def site_knowledge(repository: Repository) -> Counts:
    """This installation's history plus what other installations shared."""
    return merge(repository.site_knowledge(), shared_knowledge(exclude=repository.installation_id()))


def export(repository: Repository, path: Path | None = None) -> dict[str, Any]:
    """Replace this installation's section of the knowledge file with its current counts."""
    path = path or knowledge_path()
    payload = _read(path)
    contributions = payload.get("contributions") if isinstance(payload.get("contributions"), dict) else {}
    now = datetime.now(UTC).isoformat(timespec="seconds")
    local = repository.site_knowledge()
    contributions[repository.installation_id()] = {
        "updated_at": now,
        "sites": [{"domain": domain, **counts} for domain, counts in
                  sorted(local.items(), key=lambda item: (-item[1]["useful_reads"], item[0]))],
    }
    payload = {
        "about": "Per-site outcomes of VORA research (one section per installation), used to rank "
                 "sources. Regenerate your section with: python scripts/export_site_knowledge.py",
        "generated_at": now,
        "contributions": contributions,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload
