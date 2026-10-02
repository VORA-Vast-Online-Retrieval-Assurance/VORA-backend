from datetime import date
from unittest import TestCase
from unittest.mock import patch

import instructor

from vora.extraction.requirements import DraftConcept, PlannerDraft, align_query_years, build_requirements, upgrade_plan
from vora.research.planning import provider
from vora.research.planning.provider import heuristic_plan
from vora.shared.contracts import GoalPlan

TODAY = date(2026, 9, 29)


def required(fields: dict) -> list[str]:
    return [item.name for item in fields["concepts"] if item.role == "required"]


class PlannerTests(TestCase):
    def test_minimal_requirements_for_ev_prices(self) -> None:
        fields = build_requirements("EV car prices over the last 10 years", today=TODAY)
        self.assertEqual(required(fields), ["price", "period"])
        self.assertIn("ev", fields["subject_terms"])
        self.assertEqual(len(fields["time_scope"].periods), 10)

    def test_model_invented_columns_become_optional(self) -> None:
        draft = PlannerDraft(measures=[
            DraftConcept("price_usd", ["msrp", "average price"]), DraftConcept("make"),
            DraftConcept("model"), DraftConcept("trim_level"),
        ], dimensions=["currency", "country", "manufacturer"])
        fields = build_requirements("EV car prices over the last 10 years", draft=draft, today=TODAY)
        self.assertEqual(required(fields), ["price", "period"])
        optional = [item.name for item in fields["concepts"] if item.role == "optional"]
        self.assertIn("entity", optional)       # make / model / manufacturer
        self.assertIn("category", optional)     # trim level
        price = next(item for item in fields["concepts"] if item.name == "price")
        self.assertIn("msrp", price.aliases)

    def test_misspelt_measure_and_domain_independence(self) -> None:
        cases = {
            "Agricultural yeild in last 5 years": ["yield", "period"],
            "GPU prices past 6 months": ["price", "period"],
            "company revenue since 2019": ["revenue", "period"],
            "semiconductor shipments by quarter": ["production"],
            "housing data in Canada": [],      # no measure and no request for numbers: records or numbers
            "number of hospitals per state in India": ["value"],
            "IPL cricket team wins by season": ["value"],
            "fetch me data from egazette": [],
            "unemployment rate in Spain 2015-2020": ["rate", "period"],
            "population of India": ["population"],
        }
        for goal, expected in cases.items():
            with self.subTest(goal=goal):
                self.assertEqual(required(build_requirements(goal, today=TODAY)), expected)

    def test_model_years_are_replaced_by_the_deterministic_window(self) -> None:
        fields = build_requirements("EV prices over the last 10 years", today=TODAY)
        self.assertEqual(align_query_years("electric vehicle price history 2015-2025", fields["time_scope"]),
                         "electric vehicle price history 2017-2026")

    def test_heuristic_plan_populates_legacy_fields(self) -> None:
        plan = heuristic_plan("EV car prices over the last 10 years", today=TODAY)
        self.assertEqual(plan.required_fields, ["price", "period"])
        self.assertEqual((plan.year_start, plan.year_end), (2017, 2026))
        self.assertEqual(plan.planner, "heuristic")
        self.assertTrue(plan.search_queries)

    def test_legacy_plan_is_regrounded_against_original_goal(self) -> None:
        legacy = GoalPlan(normalized_goal="EV car price trends from 2015 to 2025",
                          required_fields=["year", "make", "model", "price_usd", "trim_level"],
                          year_start=2015, year_end=2025)
        plan = upgrade_plan(legacy, goal="Find ev car prices in last 10 years", today=TODAY)
        self.assertEqual(plan.required_fields, ["price", "period"])
        self.assertEqual((plan.year_start, plan.year_end), (2017, 2026))


class ModelCooldownTests(TestCase):
    def test_failed_model_is_skipped_until_its_cooldown_ends(self) -> None:
        calls: list[str] = []

        class Client:
            class chat:  # noqa: N801 - mirrors the instructor client shape
                class completions:  # noqa: N801
                    @staticmethod
                    def create(model: str, **_: object):
                        calls.append(model)
                        if model == "slow":
                            raise TimeoutError("timed out")
                        return provider._FieldMapping()

        provider._cooldown_until.clear()
        with patch.object(provider, "configured_models", lambda: ["slow", "backup"]), \
                patch.object(provider, "_credentials", lambda model: {}), \
                patch.object(instructor, "from_litellm", lambda completion: Client):
            self.assertEqual(provider._structured("x", provider._FieldMapping, 5)[1], "backup")
            self.assertEqual(provider._structured("x", provider._FieldMapping, 5)[1], "backup")
            self.assertEqual(calls, ["slow", "backup", "backup"])  # "slow" not retried while cooling
            self.assertEqual(provider.available_models(), ["backup"])
        provider._cooldown_until.clear()


class PlanningTimeLimitTests(TestCase):
    def test_a_slow_model_is_not_waited_for(self) -> None:
        import dataclasses
        import threading
        import time

        release = threading.Event()

        def slow_draft(goal):
            release.wait(5)
            return None

        quick = dataclasses.replace(provider.settings, llm_plan_seconds=0.2)
        with patch.object(provider, "_draft", slow_draft), patch.object(provider, "settings", quick):
            started = time.monotonic()
            plan = provider.analyze_goal("number of hospitals per state in India", today=date(2026, 9, 29))
            elapsed = time.monotonic() - started
        release.set()
        self.assertLess(elapsed, 2)
        self.assertEqual(plan.planner, "heuristic")
        self.assertIn("slower than 0.2s", plan.notes[0])


class AnswerShapeTests(TestCase):
    def setUp(self) -> None:
        from pathlib import Path

        from vora.learning import source_registry

        real = source_registry.load
        fixture = str(Path(__file__).parent / "fixtures" / "sources.json")     # the shipped registry may be empty
        patcher = patch.object(source_registry, "load", lambda path=None: real(fixture))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_requests_without_a_measure_accept_records_and_numbers(self) -> None:
        from vora.research.planning.provider import analyze_goal
        for goal, shape in {
            # a registry source with a record type: what it publishes is records
            "fetch me data from egazette": "records",
            "find digital technology expos worldwide from 2024 to 2026": "either",
            "recent RBI circulars": "either",
            "number of hospitals per state in India": "quantities",
            "EV car prices in India": "quantities",
        }.items():
            with self.subTest(goal=goal):
                plan = analyze_goal(goal, use_llm=False, today=TODAY)
                self.assertEqual(plan.answer_shape, shape)
                if shape == "either":
                    self.assertEqual([c.name for c in plan.required_concepts if c.kind == "measure"], [])
