"""A recipe read in a real browser against a local portal that behaves like an ASP.NET gazette site:
a popup to close, a "View All" postback to the listing, and a GridView paged with __doPostBack('grid','Page$N').
"""

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import TestCase, skipUnless
from urllib.parse import parse_qs

import vora.settings  # noqa: F401  (loads .env, where the browser path is configured)
from vora.learning.recipes import Recipe

import os
os.environ["VORA_NETWORK_GUARD"] = "false"  # these tests serve pages from this machine

BINARY = os.getenv("VORA_BROWSER_BINARY", "")
PER_PAGE = 3
TOTAL = 7

RECIPE = Recipe.model_validate({
    "ready_steps": [{"click": "a.cancel", "optional": True},
                    {"click_text": "View All", "optional": False, "navigates": True}],
    "table": "#gvList",
    "columns": {"Ministry": "ministry", "Subject": "subject", "Gazette ID": "gazette_id"},
    "id_field": "gazette_id",
    "id_pattern": r"^CG-DL-E-\d{8}-\d+$",
    "pagination": {"type": "postback", "pattern": "Page$", "max_pages": 5},
    "links": {"pdf_url": {"from": "gazette_id", "pattern": r"-\d{4}(\d{4})-(\d+)$",
                          "template": "https://portal.example/WriteReadData/{1}/{2}.pdf"}},
})

FORM = """<form method="post" id="f"><input type="hidden" name="__EVENTTARGET" id="t">
<input type="hidden" name="__EVENTARGUMENT" id="a">
<script>function __doPostBack(t,a){document.getElementById('t').value=t;document.getElementById('a').value=a;
document.getElementById('f').submit();}</script>{body}</form>"""


def gid(n: int) -> str:
    return f"CG-DL-E-3009202{6 if n else 6}-{27000 + n}"


class Portal(BaseHTTPRequestHandler):
    table_id = "gvList"

    def _page(self, target: str, argument: str) -> str:
        if target != "gvList" and target != "lnkAll":
            return ("<html><title>Home</title><body><div class=popup><a class=cancel href=#"
                    " onclick=\"this.parentNode.remove();return false\">x</a></div>"
                    + FORM.replace("{body}", "<a href=\"javascript:__doPostBack('lnkAll','')\">View All</a>")
                    + "</body></html>")
        page = int(argument.split("$")[1]) if argument.startswith("Page$") else 1
        start = (page - 1) * PER_PAGE
        rows = "".join(f"<tr><td>Ministry {n}</td><td>Notice {n}</td><td>{gid(n)}</td></tr>"
                       for n in range(start + 1, min(TOTAL, start + PER_PAGE) + 1))
        pages = (TOTAL + PER_PAGE - 1) // PER_PAGE
        pager = " ".join(f"<a href=\"javascript:__doPostBack('gvList','Page${n}')\">{n}</a>"
                         for n in range(1, pages + 1) if n != page)
        table = (f"<table id={self.table_id}><tr><th>Ministry</th><th>Subject</th><th>Gazette ID</th></tr>"
                 f"{rows}<tr><td colspan=3>{pager}</td></tr></table>")
        return "<html><title>Recent Uploads</title><body>" + FORM.replace("{body}", table) + "</body></html>"

    def _send(self, html: str) -> None:
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._send(self._page("", ""))

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
        self._send(self._page(form.get("__EVENTTARGET", [""])[0], form.get("__EVENTARGUMENT", [""])[0]))

    def log_message(self, *args) -> None:
        return None


@skipUnless(BINARY and Path(BINARY).is_file(), "needs a real browser (VORA_BROWSER_BINARY)")
class RecipeBrowserTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from vora.browser.engine import BrowserEngine
        from vora.browser.settings import EngineSettings

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Portal)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/"
        cls.engine = BrowserEngine(EngineSettings.from_env()).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.close()
        cls.server.shutdown()

    def setUp(self) -> None:
        Portal.table_id = "gvList"

    def test_reads_every_page_through_postbacks(self) -> None:
        run = self.engine.run_recipe(self.url, RECIPE, source_name="Test portal")
        self.assertTrue(run.ok, run.note)
        self.assertEqual(run.pages, 3)
        self.assertEqual([row.fields["gazette_id"] for row in run.rows], [gid(n) for n in range(1, TOTAL + 1)])
        first = run.rows[0]
        self.assertEqual(first.fields["pdf_url"], "https://portal.example/WriteReadData/2026/27001.pdf")
        self.assertEqual(first.method, "recipe")
        self.assertEqual(len({row.id for row in run.rows}), TOTAL)

    def test_stops_at_records_already_collected(self) -> None:
        known = {gid(n) for n in range(1, TOTAL + 1)}
        run = self.engine.run_recipe(self.url, RECIPE, known_ids=known)
        self.assertTrue(run.stopped_at_known)
        self.assertEqual(run.pages, 1)

    def test_page_limit_is_respected(self) -> None:
        self.assertEqual(self.engine.run_recipe(self.url, RECIPE, max_pages=2).pages, 2)

    def test_a_changed_site_is_reported(self) -> None:
        Portal.table_id = "somethingElse"
        run = self.engine.run_recipe(self.url, RECIPE)
        self.assertFalse(run.ok)
        self.assertIn("not found", run.note)


# ---------------------------------------------------------------------------- link collections (a news feed)

def _titles(start: int, count: int) -> list[tuple[int, str]]:
    return [(n, f"Press release number {n} on the monthly statistical return") for n in range(start, start + count)]


class Feed(BaseHTTPRequestHandler):
    """A home page with a short preview list and a 'View More' link; the full list is a one-column table of links."""

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/all"):
            rows = "".join(f'<tr><td><a href="/item?prid={n}">{title}</a></td></tr>' for n, title in _titles(100, 12))
            rows += '<tr><td><a href="/item?Id=7&Mode=0">Notification directions amendment two thousand</a></td></tr>'
            body = f"<html><title>All</title><body><table id=full>{rows}</table></body></html>"
        else:
            items = "".join(f'<li><a href="/item?prid={n}">{title}</a></li>' for n, title in _titles(100, 9))
            items += '<li><a href="/all">View More <span>items</span></a></li>'
            body = ("<html><title>Home</title><body><div class=popup><a id=close href=# "
                    "onclick=\"this.parentNode.remove();return false\">x</a></div>"
                    f"<div id=feed><ul>{items}</ul></div></body></html>")
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        return None


@skipUnless(BINARY and Path(BINARY).is_file(), "needs a real browser (VORA_BROWSER_BINARY)")
class LinkListRecipeTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from vora.browser.engine import BrowserEngine
        from vora.browser.settings import EngineSettings

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Feed)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/"
        cls.engine = BrowserEngine(EngineSettings.from_env()).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.close()
        cls.server.shutdown()

    RECIPE = Recipe.model_validate({
        "kind": "link_list",
        "ready_steps": [{"click": "#close", "optional": True},
                        {"click": "#feed li:nth-of-type(10) > a", "optional": False, "navigates": True}],
        "container": "#full", "item": "tr", "id_params": ["id", "mode", "prid"],
    })

    def test_reads_the_full_list_through_view_more(self) -> None:
        run = self.engine.run_recipe(self.url, self.RECIPE, source_name="Feed")
        self.assertTrue(run.ok, run.note)
        self.assertEqual(len(run.rows), 13)
        first = run.rows[0].fields
        self.assertEqual((first["id"], first["section"]), ("prid=100", "item"))
        self.assertTrue(first["url"].endswith("/item?prid=100"))
        self.assertIn("id=7", {row.fields["id"] for row in run.rows})        # a different parameter name is fine
        self.assertEqual(len({row.id for row in run.rows}), 13)
        self.assertNotIn("View More", " ".join(row.fields["title"] for row in run.rows))

    def test_the_same_items_stop_a_later_run_early(self) -> None:
        known = {f"prid={n}" for n in range(100, 112)} | {"id=7"}
        self.assertTrue(self.engine.run_recipe(self.url, self.RECIPE, known_ids=known).stopped_at_known)

    def test_a_changed_page_is_reported(self) -> None:
        bad = Recipe.model_validate({"kind": "link_list", "container": "#gone", "item": "tr", "id_params": ["prid"],
                                     "ready_steps": [{"click": "#close", "optional": True}]})
        run = self.engine.run_recipe(self.url, bad)
        self.assertFalse(run.ok)
        self.assertIn("not found", run.note)


class RecipeValidationTests(TestCase):
    def test_a_recipe_must_say_where_its_records_are(self) -> None:
        with self.assertRaises(Exception):
            Recipe.model_validate({"kind": "table", "id_field": "x"})
        with self.assertRaises(Exception):
            Recipe.model_validate({"kind": "link_list"})

    def test_selectors_are_bounded_single_line_text(self) -> None:
        for bad in ("a" * 500, "a\nb", "x\x00y"):
            with self.subTest(length=len(bad)):
                with self.assertRaises(Exception):
                    Recipe.model_validate({"kind": "link_list", "container": bad})
        with self.assertRaises(Exception):
            Recipe.model_validate({"kind": "link_list", "container": "#c",
                                   "ready_steps": [{"click": "a" * 500}]})
        with self.assertRaises(Exception):
            Recipe.model_validate({"kind": "link_list", "container": "#c",
                                   "ready_steps": [{"click": "#x"}] * 13})
