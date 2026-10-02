from types import MappingProxyType, SimpleNamespace
from unittest import TestCase

import dataclasses
from unittest.mock import patch

import httpx

import os

os.environ["VORA_SEARXNG_URL"] = ""   # these tests describe the built-in browser search, whatever .env says

from vora.shared.contracts import GoalPlan
from vora.research.discovery import search_api
from vora.research.discovery import discovery
from vora.research.discovery.discovery import discover, parse_search_results, region_for, search_gate

DDG = """<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.cardekho.com%2Felectric-cars">Electric Cars in India 2026</a>
<a class="result__snippet">Prices start at ₹7.99 Lakh</a></div>
<div class="result"><a class="result__a" href="https://stats.test/ev-prices">EV price statistics</a><div class="result__snippet">Annual data</div></div>
<a href="https://duckduckgo.com/settings">Settings</a>"""

BING = """<ol><li class="b_algo"><h2><a href="https://www.carwale.com/electric-cars/">CarWale electric cars</a></h2>
<div class="b_caption"><p>Latest EV prices in India</p></div></li></ol>"""


class FakeEngine:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages, self.visited = pages, []

    def execute(self, url: str):
        self.visited.append(url)
        html = next((body for key, body in self.pages.items() if key in url), "<html></html>")
        title = "Just a moment..." if "Performing security verification" in html else ""
        return SimpleNamespace(html=html, title=title, metadata=MappingProxyType({}))


class DiscoveryTests(TestCase):
    def setUp(self) -> None:
        # No spacing between searches in tests; every test starts with rested engines.
        patcher = patch.object(discovery, "settings", dataclasses.replace(
            discovery.settings, search_interval_seconds=0, max_searches_per_hour=1000))
        patcher.start()
        self.addCleanup(patcher.stop)
        search_gate.reset()
        self.addCleanup(search_gate.reset)

    def test_parse_duckduckgo_and_bing_results(self) -> None:
        ddg = parse_search_results(DDG, "duckduckgo")
        self.assertEqual([item.url for item in ddg], ["https://www.cardekho.com/electric-cars", "https://stats.test/ev-prices"])
        self.assertEqual(ddg[0].title, "Electric Cars in India 2026")
        self.assertEqual(ddg[0].snippet, "Prices start at ₹7.99 Lakh")
        self.assertEqual(ddg[1].rank, 1)
        bing = parse_search_results(BING, "bing")
        self.assertEqual(bing[0].url, "https://www.carwale.com/electric-cars/")
        self.assertEqual(bing[0].snippet, "Latest EV prices in India")

    def test_preferred_domains_get_site_searches_and_goal_links_come_first(self) -> None:
        engine = FakeEngine({"site%3Acardekho.com": DDG, "duckduckgo.com/html/?q=EV": DDG})
        plan = GoalPlan(normalized_goal="EV car prices https://example.org/ev.csv",
                        search_queries=["EV car prices India"])
        results = discover(engine, plan, 10, preferred=["cardekho.com"])
        self.assertEqual(results[0].origin, "goal")
        self.assertTrue(any("site%3Acardekho.com" in url for url in engine.visited))
        preferred = [item for item in results if item.origin == "preferred"]
        self.assertEqual([item.url for item in preferred], ["https://www.cardekho.com/electric-cars"])  # other site filtered
        self.assertIn("https://stats.test/ev-prices", [item.url for item in results])

    def test_bing_fallback_when_duckduckgo_is_empty(self) -> None:
        engine = FakeEngine({"bing.com": BING})
        results = discover(engine, GoalPlan(normalized_goal="EV prices", search_queries=["EV prices"]), 5)
        self.assertEqual([item.engine for item in results], ["bing"])

    def test_blocked_search_page_falls_back_and_is_reported(self) -> None:
        challenge = "<html><body>Performing security verification <a href='https://x.test/'>help</a></body></html>"
        engine = FakeEngine({"duckduckgo.com": challenge, "bing.com": BING})
        report: list[str] = []
        results = discover(engine, GoalPlan(normalized_goal="EV prices", search_queries=["EV prices"]), 5,
                           report=report)
        self.assertEqual([item.url for item in results], ["https://www.carwale.com/electric-cars/"])
        self.assertIn("duckduckgo: blocked by a verification page", report)
        self.assertIn("bing: 1 results", report)

    def test_consent_screen_yields_no_junk_links(self) -> None:
        consent = "<html><body>We use cookies. <a href='https://ads.test/'>Partners</a></body></html>"
        self.assertEqual(parse_search_results(consent, "bing", fallback=False), [])
        engine = FakeEngine({"duckduckgo.com": consent, "bing.com": consent})
        report: list[str] = []
        self.assertEqual(discover(engine, GoalPlan(normalized_goal="x", search_queries=["x"]), 5, report=report), [])
        self.assertEqual(report, ["duckduckgo: consent screen", "bing: consent screen"])

    def test_queries_are_interleaved_so_each_contributes(self) -> None:
        first = "".join(f'<div class="result"><a class="result__a" href="https://a{i}.test/">alpha data {i}</a></div>'
                        for i in range(2))
        second = "".join(f'<div class="result"><a class="result__a" href="https://b{i}.test/">beta data {i}</a></div>'
                         for i in range(2))
        engine = FakeEngine({"q=alpha": first, "q=beta": second})
        results = discover(engine, GoalPlan(normalized_goal="x", search_queries=["alpha", "beta"]), 4)
        self.assertEqual([item.url for item in results],
                         ["https://a0.test/", "https://b0.test/", "https://a1.test/", "https://b1.test/"])

    def test_region_hint_for_a_single_country(self) -> None:
        self.assertEqual(region_for(GoalPlan(normalized_goal="x", geography=["India"])), ("IN", "in-en"))
        self.assertIsNone(region_for(GoalPlan(normalized_goal="x", geography=["India", "Japan"])))
        self.assertIsNone(region_for(GoalPlan(normalized_goal="x", geography=["Kerala"])))
        engine = FakeEngine({})
        discover(engine, GoalPlan(normalized_goal="x", search_queries=["x"], geography=["Japan"]), 5)
        self.assertIn("&kl=jp-jp", engine.visited[0])
        self.assertIn("&cc=JP", engine.visited[1])

    def test_unrelated_results_count_as_blocked(self) -> None:
        unrelated = """<ol><li class="b_algo"><h2><a href="https://www.wimbledon.com/scores">Live Scores - Wimbledon</a></h2>
        <div class="b_caption"><p>Tennis results</p></div></li><li class="b_algo"><h2><a href="https://cloud.test/">Cloud computing</a></h2>
        <div class="b_caption"><p>Hosting and APIs</p></div></li></ol>"""
        engine = FakeEngine({"bing.com": unrelated})
        report: list[str] = []
        results = discover(engine, GoalPlan(normalized_goal="hospitals in India",
                                            search_queries=["number of hospitals per state in India"]), 5, report=report)
        self.assertEqual(results, [])
        self.assertIn("bing: blocked (results unrelated to the query)", report)

    def test_duckduckgo_bot_challenge_is_recognised(self) -> None:
        challenge = ("<html><body>Unfortunately, bots use DuckDuckGo too. Please complete the following challenge "
                     "to confirm this search was made by a human. Select all squares containing a duck:</body></html>")
        engine = FakeEngine({"duckduckgo.com": challenge, "bing.com": BING})
        report: list[str] = []
        discover(engine, GoalPlan(normalized_goal="EV prices", search_queries=["EV prices"]), 5, report=report)
        self.assertEqual(report[0], "duckduckgo: blocked by a verification page")

    def test_malformed_result_links_are_dropped(self) -> None:
        html = '<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=%2Frelative%2Fpath">EV prices</a></div>'
        self.assertEqual(parse_search_results(html, "duckduckgo"), [])

    def test_searxng_results_are_read_and_filtered(self) -> None:
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.url.params))
            return httpx.Response(200, json={"results": [
                {"url": "https://a.example/ls", "title": "A", "content": "first"},
                {"url": "javascript:alert(1)", "title": "bad", "content": ""},
                {"url": "https://b.example/", "title": "B"}]})

        configured = dataclasses.replace(search_api.settings, search_api=None, searxng_url="http://localhost:8080")
        with patch.object(search_api, "settings", configured):
            self.assertEqual(search_api.configured(), "searxng")
            found = search_api.api_search("site:a.example debates", None, httpx.MockTransport(handler))
        self.assertEqual(found, [("https://a.example/ls", "A", "first"), ("https://b.example/", "B", "")])
        self.assertEqual((seen[0]["q"], seen[0]["format"]), ("site:a.example debates", "json"))
        keyed = dataclasses.replace(search_api.settings, search_api="brave", search_api_key="k",
                                    searxng_url="http://localhost:8080")
        with patch.object(search_api, "settings", keyed):
            self.assertEqual(search_api.configured(), "brave")                  # a keyed provider wins
        with patch.object(search_api, "settings", dataclasses.replace(keyed, search_api=None, search_api_key=None, searxng_url=None)):
            self.assertIsNone(search_api.configured())

    def test_search_api_is_used_where_the_order_puts_it(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["X-Subscription-Token"], "key")
            self.assertEqual(request.url.params["country"], "in")
            return httpx.Response(200, json={"web": {"results": [
                {"url": "https://stats.test/hospitals", "title": "Hospitals by state", "description": "India"},
                {"url": "not a url", "title": "broken", "description": ""}]}})

        keyed = dataclasses.replace(search_api.settings, search_api="brave", search_api_key="key",
                                    search_order=("brave", "duckduckgo", "bing"))
        original = search_api.api_search
        with patch.object(search_api, "settings", keyed), patch.object(discovery, "settings", keyed), patch.object(
                search_api, "api_search",
                lambda query, region=None, provider=None: original(query, region, httpx.MockTransport(handler), provider)):
            engine = FakeEngine({})
            report: list[str] = []
            results = discover(engine, GoalPlan(normalized_goal="x", search_queries=["hospitals by state"],
                                                geography=["India"]), 5, report=report)
        self.assertEqual([item.url for item in results], ["https://stats.test/hospitals"])
        self.assertEqual(results[0].engine, "brave")
        self.assertEqual(engine.visited, [])  # no browser search needed
        self.assertEqual(report, ["brave: 1 results"])

    def test_the_default_order_is_duckduckgo_then_bing_then_google(self) -> None:
        self.assertEqual(search_api.settings.search_order[:3], ("duckduckgo", "bing", "google"))
        calls: list[str] = []
        keyed = dataclasses.replace(search_api.settings, search_api="google", search_api_key="key", search_api_cx="cx")
        with patch.object(search_api, "settings", keyed), patch.object(discovery, "settings", keyed), patch.object(
                search_api, "api_search", lambda query, region=None, provider=None: calls.append(provider) or [
                    ("https://stats.test/google-result", "Google result", "")]):
            # both browser engines come back empty, so Google answers last
            engine = FakeEngine({})
            report: list[str] = []
            results = discover(engine, GoalPlan(normalized_goal="x", search_queries=["hospitals by state"]), 5,
                               report=report)
        self.assertEqual([item.url for item in results], ["https://stats.test/google-result"])
        self.assertEqual(calls, ["google"])
        self.assertEqual([line.split(":")[0] for line in report], ["duckduckgo", "bing", "google"])
        # when DuckDuckGo answers, Google is never asked
        calls.clear()
        search_gate.reset()
        with patch.object(search_api, "settings", keyed), patch.object(discovery, "settings", keyed), patch.object(
                search_api, "api_search", lambda query, region=None, provider=None: calls.append(provider) or []):
            discover(FakeEngine({"duckduckgo.com": DDG}), GoalPlan(normalized_goal="EV prices", search_queries=["EV prices"]), 5)
        self.assertEqual(calls, [])

    def test_an_engine_that_blocked_us_rests(self) -> None:
        challenge = "<html><body>Unfortunately, bots use DuckDuckGo too.</body></html>"
        engine = FakeEngine({"duckduckgo.com": challenge, "bing.com": BING})
        report: list[str] = []
        discover(engine, GoalPlan(normalized_goal="EV prices", search_queries=["EV prices", "EV price list"]),
                 10, report=report)
        self.assertEqual(sum("duckduckgo.com" in url for url in engine.visited), 1)  # not asked again
        self.assertTrue(report[2].startswith("duckduckgo: skipped, resting after a block"))
        self.assertGreater(search_gate.resting("duckduckgo"), 14 * 60)
        search_gate.record("duckduckgo", blocked=True)  # a repeat block doubles the rest
        self.assertGreater(search_gate.resting("duckduckgo"), 29 * 60)
        search_gate.record("duckduckgo", blocked=False)
        self.assertEqual(search_gate.resting("duckduckgo"), 0)

    def test_hourly_search_limit(self) -> None:
        limited = dataclasses.replace(discovery.settings, search_interval_seconds=0, max_searches_per_hour=2)
        with patch.object(discovery, "settings", limited):
            engine = FakeEngine({})
            report: list[str] = []
            discover(engine, GoalPlan(normalized_goal="x", search_queries=["x", "y"]), 5, report=report)
        self.assertEqual(len(engine.visited), 2)
        self.assertIn("duckduckgo: skipped, hourly search limit reached (2)", report)
