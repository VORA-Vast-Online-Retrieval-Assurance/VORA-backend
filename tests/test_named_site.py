"""A request that names a website ("Fetch data egazette.com") must read that site.

Regression tests from a real run: the goal was answered with premierleague.com, claude.com and other
unrelated sites, because ".com" counted as a topic word, the bare domain was never opened, and the site
was not recognised as a named source.
"""

import dataclasses
from datetime import date
from types import MappingProxyType, SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from vora.extraction.scoring import ObservationScorer
from vora.research.planning.provider import analyze_goal
from vora.shared.contracts import Observation
from vora.research.discovery import discovery
from vora.research.discovery.discovery import SearchResult, discover, on_topic, search_gate
from vora.research.discovery.ranking import rank
from vora.shared.urls import goal_domains

TODAY = date(2026, 9, 30)
NO_HISTORY = {"visited": {}, "proven": {}, "blocked": {}}


def results(*urls: str) -> list[SearchResult]:
    return [SearchResult(url=url, title="", rank=index) for index, url in enumerate(urls)]


class AddressWordTests(TestCase):
    def test_a_top_level_domain_is_not_a_topic(self) -> None:
        found = on_topic(results("https://claude.com/", "https://www.premierleague.com/en/matches",
                                 "https://www.wimbledon.com/scores", "https://egazette.gov.in/"), "egazette.com")
        self.assertEqual([item.url for item in found], ["https://egazette.gov.in/"])

    def test_a_page_of_only_unrelated_results_counts_as_blocked(self) -> None:
        self.assertIsNone(on_topic(results("https://claude.com/", "https://premierleague.com/", "https://anthropic.com/"),
                                   "egazette.com"))

    def test_addresses_in_text_are_found_but_files_and_emails_are_not(self) -> None:
        self.assertEqual(goal_domains("Fetch data egazette.com"), ["egazette.com"])
        self.assertEqual(goal_domains("from https://www.egazette.gov.in/x?y=1"), ["egazette.gov.in"])
        self.assertEqual(goal_domains("see report.pdf and a@b.com, price 3.5"), [])


class NamedSiteTests(TestCase):
    """A site the registry does not know, named by its address (the generic path)."""

    def setUp(self) -> None:
        self.plan = analyze_goal("Fetch data opentenders.com", use_llm=False, today=TODAY)

    def test_the_domain_is_a_named_source(self) -> None:
        self.assertEqual(self.plan.registry_sources, [])
        self.assertEqual(self.plan.source_mentions[0], "opentenders")
        self.assertEqual(self.plan.suggested_sources, ["opentenders.com"])
        self.assertIn("opentenders official website", self.plan.search_queries)

    def test_the_site_is_opened_first_even_when_search_finds_nothing_useful(self) -> None:
        class Engine:
            visited: list[str] = []

            def execute(self, url: str):
                self.visited.append(url)
                return SimpleNamespace(html="<html></html>", title="", metadata=MappingProxyType({}))

        with patch.object(discovery, "settings", dataclasses.replace(
                discovery.settings, search_interval_seconds=0, max_searches_per_hour=1000)):
            search_gate.reset()
            found = discover(Engine(), self.plan, 10)
        self.assertEqual(found[0].url, "https://opentenders.com/")
        self.assertEqual(found[0].origin, "goal")

    def test_the_named_site_outranks_unrelated_sites_and_fills_the_batch(self) -> None:
        pages = results("https://claude.com/", "https://www.premierleague.com/", "https://opentenders.eu/a",
                        "https://opentenders.eu/b", "https://opentenders.eu/c", "https://blog.example/opentenders")
        ranked = rank(pages, self.plan, [], NO_HISTORY, max_per_domain=1, max_named_pages=8)
        self.assertEqual([item.domain for item in ranked[:3]], ["opentenders.eu"] * 3)
        self.assertTrue(any("Site name matches" in reason for reason in ranked[0].reasons))

    def test_a_code_repository_is_demoted_unless_the_request_is_about_software(self) -> None:
        repo = results("https://github.com/someone/opentenders", "https://data.example.org/notices")
        ranked = rank(repo, self.plan, [], NO_HISTORY)
        self.assertEqual(ranked[0].domain, "data.example.org")
        software = analyze_goal("popular python library repo on github", use_llm=False, today=TODAY)
        self.assertFalse(any("code repository" in reason
                             for item in rank(repo, software, [], NO_HISTORY) for reason in item.reasons))


class OffSourceRowTests(TestCase):
    def status(self, goal: str, url: str, title: str, fields: dict) -> str:
        plan = analyze_goal(goal, use_llm=False, today=TODAY)
        row = Observation(source_url=url, method="repeated_region", fields=fields, source_title=title, context=title)
        accepted, partial, rejected = ObservationScorer(plan, today=TODAY).score_all([row])
        return "accepted" if accepted else "partial" if partial else "rejected"

    def test_rows_from_an_unrelated_site_are_set_aside_not_kept_as_partial(self) -> None:
        self.assertEqual(self.status("Fetch data egazette.com", "https://www.premierleague.com/en/matches",
                                     "Premier League Fixtures", {"name": "Arsenal", "kickoff": "17:00"}), "rejected")

    def test_a_row_from_the_named_site_is_not_set_aside(self) -> None:
        self.assertNotEqual(self.status("Fetch data egazette.com", "https://egazette.gov.in/notices",
                                        "eGazette of India - Notices", {"name": "Notification 12", "kickoff": "x"}),
                            "rejected")

    def test_requests_that_name_no_site_keep_their_partial_rows(self) -> None:
        self.assertNotEqual(self.status("population by state in India", "https://example.org/x", "State overview",
                                        {"statement": "There are 3,961 villages in the state", "value": "3,961 villages"}),
                            "rejected")
