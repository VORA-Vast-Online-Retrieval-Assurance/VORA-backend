"""The rule every site is learned by: layout geometry finds repeated items whatever the markup, the accessibility tree
names regions, controls and grids, the DOM supplies selectors, and the replay check is the judge."""

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import TestCase, skipUnless
import threading

os.environ["VORA_NETWORK_GUARD"] = "false"  # these tests serve pages from this machine
import vora.settings  # noqa: E402,F401

BINARY = os.getenv("VORA_BROWSER_BINARY", "")
TITLES = ["Harvest outlook for the season", "Winter storage guidance", "River level report", "Market notes weekly",
          "Seed catalogue update"]


def respond(handler: BaseHTTPRequestHandler, body: str) -> None:
    data = body.encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/"


class Quiet(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        return None


class GeneratedClasses(Quiet):
    """Every card has its own generated class name, so no two share a signature; they share only their size."""

    def do_GET(self) -> None:  # noqa: N802
        cards = "".join(f'<div class="css-{n}x9{n}q" style="display:inline-block;width:240px;height:90px">'
                        f'<a href="/story/{n}-{t.split()[0].lower()}">{t}</a></div>' for n, t in enumerate(TITLES))
        respond(self, f'<html><title>Cards</title><body><div id=feed style="width:800px">{cards}</div></body></html>')


class SidebarBigger(Quiet):
    """A sidebar of links (more of them) beside the page's real listing (fewer): page chrome must not win."""

    def do_GET(self) -> None:  # noqa: N802
        side = "".join(f'<li><a href="/topic/{n}">Browse topic area {n} of the site</a></li>' for n in range(12))
        main = "".join(f'<li><a href="/story/{n}">{t}</a></li>' for n, t in enumerate(TITLES))
        respond(self, f'<html><title>S</title><body><div role="complementary"><ul>{side}</ul></div>'
                      f'<main><ul>{main}</ul></main></body></html>')


class IconLoadMore(Quiet):
    """The load-more control has no text at all: only an accessible name."""

    def do_GET(self) -> None:  # noqa: N802
        respond(self, """<html><title>M</title><body><table id=t><thead><tr><th>Item</th><th>Note</th></tr></thead>
<tbody id=b></tbody></table><button id=m aria-label="Show more entries" onclick="add()"><svg width=10 height=10></svg></button>
<script>let n = 0; function add(){ const b = document.getElementById('b');
 for (let i = 0; i < 4; i++) { n++; const tr = document.createElement('tr');
  tr.innerHTML = '<td>Entry ' + String.fromCharCode(64 + n) + n + '</td><td>note about entry ' + n + '</td>'; b.appendChild(tr); } }
add();</script></body></html>""")


@skipUnless(BINARY, "needs the browser")
class SiteLearningRuleTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from vora.browser.engine import BrowserEngine
        from vora.browser.settings import EngineSettings

        cls.servers = {name: serve(handler) for name, handler in
                       {"cards": GeneratedClasses, "side": SidebarBigger, "icon": IconLoadMore}.items()}
        cls.engine = BrowserEngine(EngineSettings.from_env()).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.close()
        for server, _ in cls.servers.values():
            server.shutdown()

    def learn(self, name):
        return self.engine.learn_structure(self.servers[name][1])

    def test_layout_geometry_finds_items_whose_class_names_are_all_different(self) -> None:
        learned = self.learn("cards")
        self.assertTrue(learned.ok, learned.notes)
        self.assertEqual(learned.recipe["kind"], "link_list")
        self.assertEqual(sorted(row["title"] for row in learned.sample), sorted(TITLES))

    def test_page_regions_from_the_accessibility_tree_keep_a_sidebar_out(self) -> None:
        learned = self.learn("side")
        self.assertTrue(learned.ok, learned.notes)
        self.assertEqual(sorted(row["title"] for row in learned.sample), sorted(TITLES))      # not the 12 topic links

    def test_a_control_with_only_an_accessible_name_is_a_load_more(self) -> None:
        learned = self.learn("icon")
        self.assertTrue(learned.ok, learned.notes)
        self.assertEqual(learned.recipe["pagination"]["type"], "load_more")

    def test_the_tree_is_written_onto_the_page_for_the_digest(self) -> None:
        from vora.learning.accessibility import annotate
        from vora.browser.engine import new_context

        context = new_context(self.engine)
        try:
            page = context.new_page()
            page.goto(self.servers["side"][1])
            counts = annotate(page)
            self.assertGreaterEqual(counts.get("chrome", 0), 1)
            self.assertEqual(page.eval_on_selector("[role=complementary]", "e => e.getAttribute('data-vora-ax')"), "chrome")
            page.goto(self.servers["icon"][1])
            annotate(page)
            self.assertEqual(page.eval_on_selector("#m", "e => e.getAttribute('data-vora-name')"), "Show more entries")
        finally:
            context.close()


class SummaryTests(TestCase):
    def test_the_tree_is_reduced_to_regions_grids_and_named_controls(self) -> None:
        from vora.learning.accessibility import summarize

        nodes = [
            {"role": {"value": "navigation"}, "backendDOMNodeId": 1},
            {"role": {"value": "grid"}, "backendDOMNodeId": 2},
            {"role": {"value": "button"}, "name": {"value": "Next page"}, "backendDOMNodeId": 3,
             "properties": [{"name": "disabled", "value": {"value": True}}]},
            {"role": {"value": "button"}, "name": {"value": ""}, "backendDOMNodeId": 4},
            {"role": {"value": "banner"}, "ignored": True, "backendDOMNodeId": 5},
            {"role": {"value": "generic"}, "backendDOMNodeId": 6},
        ]
        found = summarize(nodes)
        self.assertEqual(found["chrome"], [1])
        self.assertEqual(found["grids"], [2])
        self.assertEqual(found["controls"], [(3, "button", "Next page", True)])
