"""What the browser's accessibility tree says about a page, written back onto the page for the digest to read.

The tree is the browser's own account of what each element is and what it is called (from markup, ARIA and CSS). We
use it in four places only, and never to build a selector (selectors and replay stay on the DOM):

1. page regions: banner, navigation, complementary (sidebar), content info and search are page chrome, not data;
2. names of controls: the accessible name of a button or link (an icon-only "next" has no text, but has a name);
3. paging controls: their role and name, and whether they are disabled, are read from here (see ``vora.learning.structure``);
4. grids: elements the browser treats as a table, grid or tree grid, whatever tag they are.

``annotate`` marks the elements with ``data-vora-*`` attributes; the digest script reads them. When the tree cannot be
read (another browser, a closed page) it returns nothing and the DOM alone is used.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("vora.accessibility")

CHROME_ROLES = {"banner", "navigation", "complementary", "contentinfo", "search"}
GRID_ROLES = {"table", "grid", "treegrid"}
CONTROL_ROLES = {"button", "link", "tab", "menuitem"}
MAX_CHROME, MAX_GRIDS, MAX_CONTROLS = 60, 30, 500


def _value(item: Any) -> Any:
    return item.get("value") if isinstance(item, dict) else None


def summarize(nodes: list[dict]) -> dict[str, list]:
    """From a full accessibility tree: the page regions, grids and named controls (with their backend node ids)."""
    chrome, grids, controls = [], [], []
    for node in nodes:
        if node.get("ignored") or "backendDOMNodeId" not in node:
            continue
        role = _value(node.get("role")) or ""
        name = (_value(node.get("name")) or "").strip()
        properties = {p.get("name"): _value(p.get("value")) for p in node.get("properties", [])}
        backend = node["backendDOMNodeId"]
        if role in CHROME_ROLES:
            chrome.append(backend)
        elif role in GRID_ROLES:
            grids.append(backend)
        elif role in CONTROL_ROLES and name:
            controls.append((backend, role, name[:80], bool(properties.get("disabled"))))
    return {"chrome": chrome[:MAX_CHROME], "grids": grids[:MAX_GRIDS], "controls": controls[:MAX_CONTROLS]}


def annotate(page) -> dict[str, int]:
    """Mark the page's elements from its accessibility tree. Returns how many of each were marked (empty on failure)."""
    try:
        session = page.context.new_cdp_session(page)
    except Exception:  # noqa: BLE001 - not a Chromium page
        return {}
    try:
        session.send("DOM.enable")
        session.send("Accessibility.enable")
        session.send("DOM.getDocument", {"depth": 0})
        found = summarize(session.send("Accessibility.getFullAXTree").get("nodes", []))
        wanted = [*found["chrome"], *found["grids"], *[c[0] for c in found["controls"]]]
        if not wanted:
            return {"chrome": 0, "grids": 0, "controls": 0}
        pushed = session.send("DOM.pushNodesByBackendIdsToFrontend", {"backendNodeIds": wanted}).get("nodeIds", [])
        ids = dict(zip(wanted, pushed))

        def mark(backend: int, name: str, value: str) -> None:
            node = ids.get(backend)
            if node:
                session.send("DOM.setAttributeValue", {"nodeId": node, "name": name, "value": value})

        for backend in found["chrome"]:
            mark(backend, "data-vora-ax", "chrome")
        for backend in found["grids"]:
            mark(backend, "data-vora-grid", "1")
        for backend, role, name, disabled in found["controls"]:
            mark(backend, "data-vora-name", name)
            mark(backend, "data-vora-role", role)
            if disabled:
                mark(backend, "data-vora-disabled", "1")
        return {"chrome": len(found["chrome"]), "grids": len(found["grids"]), "controls": len(found["controls"])}
    except Exception as exc:  # noqa: BLE001 - the DOM-only digest still works
        logger.debug("accessibility tree not read: %s", exc)
        return {}
    finally:
        try:
            session.detach()
        except Exception:  # noqa: BLE001
            pass
