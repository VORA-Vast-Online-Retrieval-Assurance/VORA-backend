"""Export what this installation learned about websites to data/site_knowledge.json.

Run from the repository root, then commit the file:

    python scripts/export_site_knowledge.py

Only per-site counts are written (reads, useful reads, accepted rows, blocks):
no goals, page addresses, conversations or data. The file has one section per
installation; running this again replaces your own section, so several
developers can contribute without double counting.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vora.settings import settings  # noqa: E402
from vora.research.discovery.knowledge import export, knowledge_path, merge, shared_knowledge  # noqa: E402
from vora.storage.repository import Repository  # noqa: E402


def main() -> None:
    repository = Repository(settings.resolved_database_path())
    payload = export(repository)
    own = payload["contributions"][repository.installation_id()]["sites"]
    everyone = merge(shared_knowledge())
    print(f"Wrote {knowledge_path()}")
    print(f"  your section: {len(own)} sites ({sum(1 for site in own if site['useful_reads'])} gave data, "
          f"{sum(1 for site in own if site['blocked_reads'])} blocked at least once)")
    print(f"  all installations: {len(everyone)} sites from {len(payload['contributions'])} installation(s)")


if __name__ == "__main__":
    main()
