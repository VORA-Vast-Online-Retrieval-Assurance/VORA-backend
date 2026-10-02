"""Reading a listing from the paged data service behind a page (vora/learning/api_records.py)."""

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

os.environ["VORA_NETWORK_GUARD"] = "false"  # these tests serve pages from this machine
import vora.settings  # noqa: E402,F401
from vora.learning import api_records as api  # noqa: E402
from vora.learning.recipes import Recipe, run_recipe  # noqa: E402

TOTAL = 45


def record(n):
    return {"house": 18, "session": 8, "slno": n, "title": f"Debate {n}", "members": ["A", "B"], "extra": None,
            "type": {"id": 3, "name": "Ruling"}, "rowNo": n}


class Service(BaseHTTPRequestHandler):
    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        page, size = int(query.get("page", ["1"])[0]), int(query.get("size", ["10"])[0])
        rows = [record(n) for n in range(1, TOTAL + 1)][(page - 1) * size: page * size]
        body = json.dumps({"_metadata": {"totalPages": -(-TOTAL // size)}, "records": rows}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class PureTests(unittest.TestCase):
    def test_names_and_flattening(self) -> None:
        self.assertEqual(api.snake("debateTitle"), "debate_title")
        flat = api.flatten(record(1))
        self.assertEqual(flat["members"], "A; B")
        self.assertEqual(flat["type_name"], "Ruling")
        self.assertNotIn("extra", flat)

    def test_the_records_list_is_found_inside_a_wrapper(self) -> None:
        path, items = api.find_records({"_metadata": {"n": 1}, "records": [record(n) for n in range(5)], "menu": [{"a": 1}]})
        self.assertEqual(path, ["records"])
        self.assertEqual(len(items), 5)
        self.assertIsNone(api.find_records({"labels": {"a": "b"}, "x": [1, 2, 3]}))

    def test_paging_parameters_are_recognised_by_role(self) -> None:
        self.assertEqual(api.paging_of("https://x.example/s?q=&page=1&size=10"),
                         {"page": "page", "start": 1, "offset": False, "size": "size", "size_value": 10})
        self.assertEqual(api.paging_of("https://x.example/s?start=0&rows=50")["offset"], True)
        self.assertIsNone(api.paging_of("https://x.example/labels?locale=en"))

    def test_a_record_is_identified_by_what_its_link_is_built_from_else_by_a_unique_key(self) -> None:
        items = [record(n) for n in range(1, 6)]
        self.assertEqual(api.identify(items, ["house", "session", "slno"]), ["house", "session", "slno"])
        self.assertEqual(api.identify(items), ["slno"])                # the row number 'rowNo' ranks last
        self.assertEqual(api.identify([{"a": 1, "b": 1}, {"a": 1, "b": 2}]), ["b"])

    def test_the_link_template_comes_from_an_address_on_the_page(self) -> None:
        found = api.link_template(["/about", "/ls/view?ls=18&session=8&dbslno=3"], record(3), "https://x.example/ls/list")
        self.assertEqual(found, ("https://x.example/ls/view?ls={house}&session={session}&dbslno={slno}",
                                 ["house", "session", "slno"]))
        self.assertIsNone(api.link_template(["/ls/view?id=999"], record(3), "https://x.example/"))


class ReadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), Service)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.engine = SimpleNamespace(settings=SimpleNamespace(guard_network=False, navigation_timeout_ms=5000))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def recipe(self, **extra):
        # the model allows https only; the test server is plain http, so the address is set after validation
        recipe = Recipe.model_validate({
            "kind": "json_api", "api_url": "https://placeholder.example/s?page=1&size=10", "records_path": ["records"],
            "id_fields": ["house", "session", "slno"], "page_param": "page", "size_param": "size", "api_size": 10,
            "total_pages_path": ["_metadata", "totalPages"], "pagination": {"type": "none", "max_pages": 50},
            "link_template": "https://x.example/view?ls={house}&session={session}&dbslno={slno}",
            "id_fields_link": ["house", "session", "slno"], **extra})
        object.__setattr__(recipe, "api_url", self.base + "/s?page=1&size=10")
        return recipe

    def test_every_page_is_read_until_the_service_runs_out(self) -> None:
        run = run_recipe(self.engine, "https://x.example/ls/list", self.recipe())
        self.assertTrue(run.ok)
        self.assertEqual((run.pages, len(run.rows)), (5, TOTAL))
        first = run.rows[0].fields
        self.assertEqual(first["id"], "house=18|session=8|slno=1")
        self.assertEqual(first["url"], "https://x.example/view?ls=18&session=8&dbslno=1")
        self.assertEqual(len({row.id for row in run.rows}), TOTAL)

    def test_a_page_cap_and_known_records_stop_the_read(self) -> None:
        self.assertEqual(len(run_recipe(self.engine, "https://x.example/", self.recipe(), max_pages=2).rows), 20)
        known = {f"house=18|session=8|slno={n}" for n in range(1, 11)}
        run = run_recipe(self.engine, "https://x.example/", self.recipe(), known_ids=known)
        self.assertTrue(run.ok)
        again = run_recipe(self.engine, "https://x.example/", self.recipe(),
                           known_ids={f"house=18|session=8|slno={n}" for n in range(1, TOTAL + 1)})
        self.assertTrue(again.stopped_at_known)
        self.assertEqual(again.pages, 1)

    def test_a_recipe_must_name_its_data_address_and_identifier(self) -> None:
        with self.assertRaises(ValueError):
            Recipe.model_validate({"kind": "json_api", "api_url": "ftp://plain.example/x", "id_fields": ["a"]})
        with self.assertRaises(ValueError):
            Recipe.model_validate({"kind": "json_api", "api_url": "https://ok.example/x"})
        with self.assertRaises(ValueError):
            Recipe.model_validate({"kind": "json_api", "api_url": "https://ok.example/x", "id_fields": ["a"],
                                   "page_param": "p\nq"})


PAGE = b"""<html><head><title>Debates</title></head><body><h1>Debates</h1>
<table id="t"><thead><tr><th>S. No</th><th>Title</th><th>Date</th></tr></thead><tbody></tbody></table>
<a href="/view?house=18&session=8&slno=1">first</a>
<script>
fetch('/data?page=1&size=10').then(r => r.json()).then(d => {
  const body = document.querySelector('tbody');
  d.records.forEach((r, i) => { const tr = document.createElement('tr');
    tr.innerHTML = '<td>' + (i + 1) + '</td><td>' + r.title + '</td><td>2026-01-0' + (i % 9 + 1) + '</td>'; body.appendChild(tr); });
});
</script></body></html>"""


class Site(Service):
    def do_GET(self):
        if self.path.startswith("/data"):
            return super().do_GET()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(PAGE)


@unittest.skipUnless(os.getenv("VORA_BROWSER_BINARY"), "needs the browser")
class LearnTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from vora.browser.engine import BrowserEngine
        from vora.browser.settings import EngineSettings

        cls.server = HTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/"
        cls.engine = BrowserEngine(EngineSettings.from_env()).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.close()
        cls.server.shutdown()
        cls.server.server_close()

    def test_a_table_without_an_id_column_is_learned_from_the_service_behind_it(self) -> None:
        learned = self.engine.learn_structure(self.url)
        self.assertTrue(learned.ok, learned.notes)
        recipe = learned.recipe
        self.assertEqual(recipe["kind"], "json_api")
        self.assertEqual(recipe["records_path"], ["records"])
        self.assertEqual(recipe["id_fields"], ["house", "session", "slno"])      # what the page's own link is built from
        self.assertEqual(recipe["page_param"], "page")
        self.assertGreaterEqual(recipe["api_size"], 10)
        self.assertEqual(len(learned.sample), TOTAL)


if __name__ == "__main__":
    unittest.main()
