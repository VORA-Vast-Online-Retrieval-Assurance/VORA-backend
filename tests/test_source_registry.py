"""The source registry: requests naming an official portal go to it, without depending on search."""

import json
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType, SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from vora.learning.recipes import Recipe, build_links, canonical_url, natural_id, rows_from_table
from vora.research.planning.provider import analyze_goal
from vora.research.discovery import discovery
from vora.learning import source_registry
from vora.research.discovery.discovery import discover, search_gate
from vora.research.discovery.ranking import rank

FIXTURE = str(Path(__file__).parent / "fixtures" / "sources.json")   # the shipped registry is data and may be empty
_REAL_LOAD = source_registry.load
_patch = patch.object(source_registry, "load", lambda path=None: _REAL_LOAD(path or FIXTURE))


def setUpModule() -> None:
    _patch.start()


def tearDownModule() -> None:
    _patch.stop()


TODAY = date(2026, 9, 30)
NO_HISTORY = {"visited": {}, "proven": {}, "blocked": {}}


class RegistryMatchTests(TestCase):
    def test_every_way_of_naming_the_gazette_reaches_the_official_site(self) -> None:
        for goal in ("egazette", "Fetch data egazette.com", "fetch me data from egazette",
                     "latest gazette of india notifications", "e-Gazette notifications this week",
                     "data from egazette.gov.in", "E Gazette notices"):
            with self.subTest(goal=goal):
                plan = analyze_goal(goal, use_llm=False, today=TODAY)
                self.assertEqual(plan.registry_sources, ["egazette-india"])
                self.assertEqual(plan.suggested_sources[0], "egazette.gov.in")
                self.assertEqual(plan.answer_shape, "records")

    def test_unrelated_requests_match_nothing(self) -> None:
        for goal in ("EV car prices in India", "population of India", "gazelle migration data", "gazebo prices"):
            with self.subTest(goal=goal):
                self.assertEqual(analyze_goal(goal, use_llm=False, today=TODAY).registry_sources, [])

    def test_a_bad_entry_is_skipped_and_the_rest_still_load(self) -> None:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "sources.json"
            path.write_text(json.dumps({"sources": [
                {"id": "broken", "name": "No domains"},
                {"id": "ok", "name": "Portal", "aliases": ["portalx"], "domains": ["portal.example"],
                 "entry": "https://portal.example/"},
            ]}), encoding="utf-8")
            entries = source_registry.load(str(path))
        self.assertEqual([entry.id for entry in entries], ["ok"])

    def test_the_registry_files_are_valid(self) -> None:
        entries = [*_REAL_LOAD(FIXTURE), *_REAL_LOAD()]
        self.assertTrue(entries)
        self.assertTrue(all(entry.entry.startswith("https://") for entry in entries))


class RegistryDiscoveryTests(TestCase):
    def test_the_official_site_comes_first_even_when_search_is_blocked(self) -> None:
        class Engine:
            def execute(self, url: str):
                return SimpleNamespace(html="<html><title>Just a moment...</title>Performing security verification</html>",
                                       title="Just a moment...", metadata=MappingProxyType({}))

        plan = analyze_goal("Fetch data egazette.com", use_llm=False, today=TODAY)
        search_gate.reset()
        self.addCleanup(search_gate.reset)
        with patch.object(discovery, "settings", discovery.settings.__class__(
                **{**{name: getattr(discovery.settings, name) for name in discovery.settings.__slots__},
                   "search_interval_seconds": 0, "max_searches_per_hour": 1000})):
            found = discover(Engine(), plan, 10)
        self.assertEqual((found[0].url, found[0].origin), ("https://egazette.gov.in/", "registry"))
        # the misnamed address the user typed is not opened; the official site is
        self.assertNotIn("https://egazette.com/", [item.url for item in found])
        ranked = rank(found, plan, [], NO_HISTORY)
        self.assertEqual(ranked[0].url, "https://egazette.gov.in/")
        self.assertTrue(any(reason.startswith("Official source") for reason in ranked[0].reasons))


class RecipeTableTests(TestCase):
    def setUp(self) -> None:
        self.recipe = source_registry.by_id("egazette-india").recipe

    def table(self, headers, *rows):
        return {"headers": headers, "rows": [[{"text": text, "href": ""} for text in row] for row in rows]}

    def test_rows_map_to_fields_with_a_pdf_link_built_from_the_id(self) -> None:
        headers = ["S. No.", "Ministry / Organization", "Department", "Office", "Subject", "Part & Section",
                   "Issue Date", "Publish Date", "Gazette ID", "Download"]
        rows, problem = rows_from_table(self.table(headers, ["1.", "Ministry of Finance", "Revenue", "CBIC",
                                                              "Tariff Value Notification", "Part II", "30-Sep-2026",
                                                              "30-Sep-2026", "CG-DL-E-30092026-276649", "2.4 MB"]),
                                        self.recipe)
        self.assertEqual(problem, "")
        self.assertEqual(rows[0]["ministry"], "Ministry of Finance")
        self.assertEqual(rows[0]["pdf_url"], "https://egazette.gov.in/WriteReadData/2026/276649.pdf")

    def test_a_changed_site_is_reported_not_guessed(self) -> None:
        self.assertEqual(rows_from_table(None, self.recipe), ([], "table #gvGazetteList not found"))
        rows, problem = rows_from_table(self.table(["Name", "Date"], ["x", "y"]), self.recipe)
        self.assertEqual(rows, [])
        self.assertIn("gazette_id", problem)
        rows, problem = rows_from_table(self.table(["Gazette ID"], ["not-an-id"]), self.recipe)
        self.assertIn("identifiers no longer match", problem)

    def test_links_ids_and_urls(self) -> None:
        recipe = Recipe.model_validate({"table": "t", "columns": {"ID": "id"}, "id_field": "id", "links": {
            "doc": {"from": "id", "pattern": r"(\d+)$", "template": "https://x.example/{1}.pdf"}}})
        self.assertEqual(build_links({"id": "A-17"}, recipe), {"doc": "https://x.example/17.pdf"})
        self.assertNotEqual(natural_id("a.example", "id", "X-1"), natural_id("a.example", "id", "X-2"))
        self.assertEqual(canonical_url("https://egazette.gov.in/(S(abc123))/RecentUploads.aspx?Category=6"),
                         "https://egazette.gov.in/RecentUploads.aspx?Category=6")


class NaturalKeyTests(TestCase):
    def test_the_same_notice_from_two_pages_is_one_row(self) -> None:
        from vora.shared.contracts import Observation

        first = Observation(source_url="https://egazette.gov.in/(S(a))/Home.aspx", method="html_table",
                            fields={"subject": "Tariff", "gazette_id": "CG-DL-E-30092026-276649"})
        second = Observation(source_url="https://egazette.gov.in/RecentUploads.aspx", method="recipe",
                             fields={"subject": "Tariff value notification", "gazette_id": "CG-DL-E-30092026-276649"})
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.id, natural_id("egazette.gov.in", "gazette_id", "CG-DL-E-30092026-276649"))

    def test_row_numbers_are_not_identifiers(self) -> None:
        from vora.shared.contracts import Observation

        one = Observation(source_url="https://shop.example/a", method="html_table", fields={"id": "24", "name": "A"})
        two = Observation(source_url="https://shop.example/b", method="html_table", fields={"id": "24", "name": "B"})
        self.assertNotEqual(one.id, two.id)


class HydrationTests(TestCase):
    def test_records_embedded_by_a_javascript_app_are_read(self) -> None:
        from types import MappingProxyType as Frozen

        from vora.browser.contracts import ExecutionResult
        from vora.extraction.parser import parse_rendered_page

        data = {"props": {"pageProps": {"events": [
            {"name": f"Expo {n}", "city": "Berlin", "startDate": f"2026-0{n}-10", "url": f"https://expo{n}.example"}
            for n in range(1, 5)], "menu": [{"label": "Home"}]}}}
        html = (f'<html><head><title>Events</title></head><body><div id="__next"></div>'
                f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></body></html>')
        result = ExecutionResult(execution_id="h", requested_url="https://events.example/", final_url="https://events.example/",
                                 title="Events", html=html, status=200, elapsed_seconds=0.1, network_idle_reached=True,
                                 metadata=Frozen({"fetched_at": "2026-09-30T08:00:00+00:00"}))
        rows = [row for row in parse_rendered_page(result) if row.method == "hydration_json"]
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0].fields["name"], "Expo 1")


class RegistryOutcomeTests(TestCase):
    def test_a_page_found_through_the_registry_can_be_recorded(self) -> None:
        from vora.shared.contracts import SourceOutcome

        outcome = SourceOutcome(id="x", url="https://egazette.gov.in/", status="complete", origin="registry")
        self.assertEqual(outcome.origin, "registry")
