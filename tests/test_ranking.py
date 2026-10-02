from datetime import UTC, date, datetime, timedelta
from unittest import TestCase

from vora.extraction.requirements import build_requirements
from vora.shared.contracts import GoalPlan
from vora.research.discovery.discovery import SearchResult
from vora.research.discovery.ranking import rank

TODAY = date(2026, 9, 29)


def plan_for(goal: str, suggested: list[str] | None = None) -> GoalPlan:
    fields = build_requirements(goal, today=TODAY)
    fields.pop("corrected_goal")
    fields["suggested_sources"] = suggested or []
    return GoalPlan(normalized_goal=goal, **fields)


def result(url: str, rank_index: int, title: str = "", origin: str = "search") -> SearchResult:
    return SearchResult(url=url, title=title, rank=rank_index, origin=origin)


NO_HISTORY = {"visited": {}, "proven": {}, "blocked": {}}


class RankingTests(TestCase):
    def setUp(self) -> None:
        self.plan = plan_for("EV car prices in India")

    def test_preferred_source_beats_search_order(self) -> None:
        results = [result("https://news.test/ev", 0), result("https://www.cardekho.com/electric-cars", 5)]
        ranked = rank(results, self.plan, ["cardekho.com"], NO_HISTORY)
        self.assertEqual(ranked[0].domain, "cardekho.com")
        self.assertIn("Preferred source (cardekho.com)", ranked[0].reasons)

    def test_one_page_per_site_before_any_site_repeats(self) -> None:
        results = [result("https://a.test/1", 0), result("https://a.test/2", 1), result("https://b.test/1", 2)]
        ranked = rank(results, self.plan, [], NO_HISTORY)
        self.assertEqual([item.domain for item in ranked], ["a.test", "b.test", "a.test"])
        self.assertTrue(any("ranked higher" in reason for reason in ranked[2].reasons))

    def test_recently_blocked_site_is_demoted(self) -> None:
        blocked = {"visited": {}, "proven": {}, "blocked": {"iea.org": (datetime.now(UTC) - timedelta(days=2)).isoformat()}}
        ranked = rank([result("https://www.iea.org/ev", 0), result("https://other.test/ev", 3)], self.plan, [], blocked)
        self.assertEqual(ranked[0].domain, "other.test")
        self.assertIn("Blocked us 2 days ago", ranked[1].reasons)

    def test_rotation_and_proven_sources(self) -> None:
        now = datetime.now(UTC)
        history = {"visited": {"https://old.test/page": {"accepted": 0, "at": now.isoformat()},
                               "https://good.test/a": {"accepted": 12, "at": now.isoformat()}},
                   "proven": {"good.test": 12}, "blocked": {}}
        results = [result("https://old.test/page", 0), result("https://new.test/page", 1),
                   result("https://good.test/a", 2), result("https://good.test/b", 3)]
        ranked = rank(results, self.plan, [], history, max_per_domain=2)
        order = [item.url for item in ranked]
        self.assertEqual(order[0], "https://good.test/b")  # unread page on a proven site first
        self.assertLess(order.index("https://new.test/page"), order.index("https://good.test/a"))  # rotation
        self.assertLess(order.index("https://new.test/page"), order.index("https://old.test/page"))
        self.assertTrue(any("unread pages first" in reason for reason in ranked[order.index("https://good.test/a")].reasons))

        # After the revisit window a useful page is refreshed again.
        history["visited"]["https://good.test/a"]["at"] = (now - timedelta(hours=30)).isoformat()
        refreshed = rank([result("https://good.test/a", 0)], self.plan, [], history)
        self.assertTrue(any(reason.startswith("Refreshing") for reason in refreshed[0].reasons))

    def test_planner_suggestions_and_data_signals(self) -> None:
        plan = plan_for("EV car prices in India", suggested=["carwale.com"])
        ranked = rank([result("https://blog.test/ev-thoughts", 0, "My thoughts on EVs"),
                       result("https://www.carwale.com/electric-cars/", 1, "Electric car price list 2026")],
                      plan, [], NO_HISTORY)
        self.assertEqual(ranked[0].domain, "carwale.com")
        self.assertIn("Suggested by the planner", ranked[0].reasons)
        self.assertIn("Looks like a data page", ranked[0].reasons)

    def test_files_are_not_ranked_as_pages(self) -> None:
        ranked = rank([result("https://stats.test/report.pdf", 0), result("https://stats.test/page", 1)],
                      self.plan, [], NO_HISTORY)
        self.assertEqual([item.url for item in ranked], ["https://stats.test/page"])
