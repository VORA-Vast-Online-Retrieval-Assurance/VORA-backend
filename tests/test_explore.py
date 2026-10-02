import os
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs
from pathlib import Path
from unittest import TestCase, skipUnless

import vora.settings  # noqa: F401  (loads .env, where the browser path is configured)
from vora.browser.engine import BrowserEngine
from vora.browser.events import EventBus, LifecycleEvent
from vora.browser.explore import ExploreHints, is_safe_action
from vora.browser.settings import EngineSettings

import os
os.environ["VORA_NETWORK_GUARD"] = "false"  # these tests serve pages from this machine

PAGE = "https://stats.test/rainfall"


class SafetyTests(TestCase):
    def test_controls_that_only_change_the_view_are_allowed(self) -> None:
        for text in ("Load more", "Show all", "2023", "Monthly", "View more results", "Next"):
            self.assertTrue(is_safe_action(text, "", PAGE), text)
        self.assertTrue(is_safe_action("Next", "/rainfall?page=2", PAGE))
        self.assertTrue(is_safe_action("Details", "#details", PAGE))

    def test_risky_controls_are_refused(self) -> None:
        for text in ("Buy now", "Add to cart", "Sign in", "Log out", "Download PDF", "Subscribe",
                     "Delete", "Apply now", "Share on Facebook", "Contact us"):
            self.assertFalse(is_safe_action(text, "", PAGE), text)
        self.assertFalse(is_safe_action("Show more", "", PAGE, in_form=True))
        self.assertFalse(is_safe_action("Search", "", PAGE, input_type="submit"))
        self.assertFalse(is_safe_action("Next", "https://other.test/page2", PAGE))
        self.assertFalse(is_safe_action("Full table", "/files/table.xlsx", PAGE))
        self.assertFalse(is_safe_action("", "", PAGE))


INDEX = """<!doctype html><html><head><title>Rainfall data</title></head><body>
<main>
  <h1>Rainfall data</h1>
  <details><summary>Archive</summary><table><tr><th>Month</th><th>Rain</th></tr><tr><td>Row-A</td><td>1</td></tr></table></details>
  <div role="tablist">
    <button role="tab" aria-selected="true" id="t1" onclick="pick(1)">2024</button>
    <button role="tab" aria-selected="false" id="t2" onclick="pick(2)">2023</button>
  </div>
  <div id="p1">Current year shown</div>
  <div id="p2" hidden><table><tr><td>Row-B</td><td>2</td></tr></table></div>
  <table id="list"><tr><td>Row-first</td></tr></table>
  <button id="more" onclick="more()">Load more</button>
  <button onclick="document.body.insertAdjacentHTML('beforeend','<p>BOUGHT</p>')">Buy now</button>
  <form><button type="submit" onclick="document.body.insertAdjacentHTML('beforeend','<p>SUBMITTED</p>');return false;">Show more</button></form>
  <iframe src="/frame.html" width="400" height="200"></iframe>
  <rain-card></rain-card>
  <div id="chart"></div>
  <a rel="next" href="/page2.html">Next</a>
</main>
<script>
  function pick(n) {
    for (const i of [1, 2]) {
      document.getElementById('p' + i).hidden = i !== n;
      document.getElementById('t' + i).setAttribute('aria-selected', String(i === n));
    }
  }
  function more() {
    document.getElementById('list').insertAdjacentHTML('beforeend', '<tr><td>Row-C</td></tr>');
    document.getElementById('more').remove();
  }
  customElements.define('rain-card', class extends HTMLElement {
    connectedCallback() { this.attachShadow({mode: 'open'}).innerHTML = '<table><tr><td>Row-E</td><td>5</td></tr></table>'; }
  });
  fetch('/data.json').then((r) => r.json()).then((d) => { document.getElementById('chart').dataset.points = d.length; });
</script>
</body></html>"""

FILES = {
    "/index.html": ("text/html", INDEX),
    "/frame.html": ("text/html", "<html><body><table><tr><th>Station</th><th>Rain</th></tr>"
                                  "<tr><td>Row-D embedded station reading table</td><td>4</td></tr></table>"
                                  "<p>Embedded rainfall table with station readings for the whole state "
                                  "covering every district and month of the monitoring season.</p></body></html>"),
    "/page2.html": ("text/html", "<html><head><title>Rainfall page 2</title></head><body><main>"
                                  "<table><tr><td>Row-F</td><td>6</td></tr></table></main></body></html>"),
    "/data.json": ("application/json", '[{"month": "2024-06", "rain": 648.3}, {"month": "2024-07", "rain": 653.5}]'),
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        content_type, body = FILES.get(self.path, ("text/plain", "missing"))
        payload = body.encode()
        self.send_response(200 if self.path in FILES else 404)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        return None


BINARY = os.getenv("VORA_BROWSER_BINARY", "")


@skipUnless(BINARY and Path(BINARY).is_file(), "needs a real browser (VORA_BROWSER_BINARY)")
class ExploreBrowserTests(TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_hidden_content_is_revealed_safely(self) -> None:
        events = EventBus()
        completed = []
        events.subscribe(LifecycleEvent.EXECUTION_COMPLETE, lambda event: completed.append(event.data["result"]))
        with BrowserEngine(EngineSettings.from_env(), events) as engine:
            exploration = engine.explore(f"{self.base}/index.html", budget_seconds=40)
        html = "\n".join(state.html for state in exploration.states)
        for marker in ("Row-A", "Row-B", "Row-C", "Row-D", "Row-E", "Row-F"):
            self.assertIn(marker, html, f"{marker} not revealed; actions: {exploration.actions}")
        self.assertNotIn("<p>BOUGHT</p>", html)  # the button was never clicked
        self.assertNotIn("<p>SUBMITTED</p>", html)
        self.assertEqual([item.url.rsplit("/", 1)[-1] for item in exploration.captured], ["data.json"])
        self.assertEqual(len(completed), len(exploration.states))
        labels = [state.metadata["state"] for state in exploration.states]
        self.assertIn("tab: 2023", labels)
        self.assertTrue(any(label.startswith("page ") for label in labels))


PORTAL_ROWS_PER_PAGE = 3
PORTAL_PAGES = 3


def portal_page(page: int | None) -> str:
    """A registry built like an ASP.NET WebForms page: one page-wide form, a search
    button, and a grid whose pager links are postbacks (not addresses)."""
    grid = ""
    if page:
        rows = "".join(
            f"<tr><td>Ministry {page}-{n}</td><td>Notice P{page}R{n} regarding rules</td>"
            f"<td>CG-DL-E-{page}{n}092026-2683{page}{n}</td><td>0{page}-Sep-2026</td>"
            f'<td><a href="/WriteReadData/2026/P{page}R{n}.pdf">Download</a></td></tr>'
            for n in range(1, PORTAL_ROWS_PER_PAGE + 1))
        pager = "".join(
            (f"<span>{number}</span>" if number == page else
             f"<a href=\"javascript:__doPostBack('grid','Page${number}')\">{number}</a>")
            for number in range(1, PORTAL_PAGES + 1))
        grid = (f'<table id="grid"><tr><th>Ministry</th><th>Subject</th><th>Gazette ID</th><th>Issue Date</th>'
                f'<th>Download</th></tr>{rows}</table><div class="pager">{pager}</div>')
    return f"""<!doctype html><html><head><title>Search Gazette</title></head><body>
<div class="menu"><a href="/portal">Home</a><a href="/portal">Search Gazette</a></div>
<form id="aspnetForm" method="post" action="/portal">
  <input type="hidden" name="__EVENTTARGET" value=""><input type="hidden" name="__EVENTARGUMENT" value="">
  <input type="hidden" name="__VIEWSTATE" value="abc123">
  <table><tr><td>Date from</td><td><input type="text" name="txtDateFrom" placeholder="dd/mm/yyyy"></td></tr>
  <tr><td>Date to</td><td><input type="text" name="txtDateTo" placeholder="dd/mm/yyyy"></td></tr>
  <tr><td>Category</td><td><select name="ddlCategory"><option value="">-- Select --</option>
      <option value="1">Extraordinary</option><option value="2">Weekly</option></select></td></tr></table>
  <input type="submit" name="btnSearch" value="Search">
  {grid}
</form>
<form method="post" action="/login"><input name="user"><input type="password" name="pwd">
  <input type="submit" value="Sign in"></form>
<form method="post" action="/subscribe"><input type="email" name="email"><input type="submit" value="Subscribe">
  Subscribe to our newsletter</form>
<form method="post" action="/contact"><input type="email" name="email"><textarea name="message"></textarea>
  <input type="submit" value="Send"></form>
<script>function __doPostBack(target, argument) {{
  const f = document.getElementById('aspnetForm');
  f.__EVENTTARGET.value = target; f.__EVENTARGUMENT.value = argument; f.submit(); }}</script>
</body></html>"""


class _PortalHandler(BaseHTTPRequestHandler):
    def _send(self, body: str, status: int = 200) -> None:
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        self._send(portal_page(None) if self.path.startswith("/portal") else "<html>missing</html>",
                   200 if self.path.startswith("/portal") else 404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = {key: values[0] for key, values in parse_qs(self.rfile.read(length).decode()).items()}
        self.server.posts.append((self.path, body))
        if self.path != "/portal":
            self._send("<html><body>done</body></html>")
            return
        argument = body.get("__EVENTARGUMENT", "")
        page = int(argument.split("$")[1]) if argument.startswith("Page$") else 1
        self._send(portal_page(page))

    def log_message(self, *args) -> None:
        return None


@skipUnless(BINARY and Path(BINARY).is_file(), "needs a real browser (VORA_BROWSER_BINARY)")
class QueryFormBrowserTests(TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _PortalHandler)
        self.server.posts = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/portal"

    def explore(self, hints):
        with BrowserEngine(EngineSettings.from_env(), EventBus()) as engine:
            return engine.explore(self.url, budget_seconds=60, hints=hints)

    def test_search_form_is_submitted_and_postback_pages_are_followed(self) -> None:
        hints = ExploreHints(window=(date(2026, 9, 1), date(2026, 9, 30)))
        exploration = self.explore(hints)
        html = "\n".join(state.html for state in exploration.states)
        for page in range(1, PORTAL_PAGES + 1):
            for row in range(1, PORTAL_ROWS_PER_PAGE + 1):
                self.assertIn(f"Notice P{page}R{row}", html, f"page {page} row {row}; {exploration.actions}")
        searched = [body for path, body in self.server.posts if path == "/portal" and body.get("btnSearch")]
        self.assertEqual(len(searched), 1)
        self.assertEqual((searched[0]["txtDateFrom"], searched[0]["txtDateTo"]), ("01/09/2026", "30/09/2026"))
        self.assertEqual(searched[0].get("ddlCategory", ""), "")  # nothing in the request names a category

    def test_forms_that_change_state_are_never_submitted(self) -> None:
        self.explore(ExploreHints(window=(date(2026, 9, 1), date(2026, 9, 30))))
        touched = {path for path, _ in self.server.posts}
        self.assertEqual(touched, {"/portal"}, f"submitted: {touched}")

    def test_forms_can_be_switched_off(self) -> None:
        exploration = self.explore(ExploreHints(forms=False))
        self.assertEqual(self.server.posts, [])
        self.assertFalse(any("Notice P1R1" in state.html for state in exploration.states))

    def test_the_records_behind_the_form_are_accepted_as_a_dataset(self) -> None:
        from datetime import date as day
        from types import MappingProxyType
        from vora.extraction.parser import parse_rendered_page
        from vora.extraction.scoring import ObservationScorer
        from vora.research.planning.provider import analyze_goal

        exploration = self.explore(ExploreHints(window=(day(2026, 9, 1), day(2026, 9, 30))))
        plan = analyze_goal("fetch me data from egazette", use_llm=False, today=day(2026, 9, 30))
        accepted = {}
        for state in exploration.states:
            for row in ObservationScorer(plan, today=day(2026, 9, 30)).score_all(parse_rendered_page(state))[0]:
                accepted[row.id] = row
        subjects = {value for row in accepted.values() for value in row.fields.values() if value.startswith("Notice P")}
        self.assertEqual(len(subjects), PORTAL_ROWS_PER_PAGE * PORTAL_PAGES)
        self.assertTrue(all(any(".pdf" in value for value in row.fields.values()) for row in accepted.values()))
