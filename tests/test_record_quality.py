"""Records must describe the thing asked for, not a site's own bookkeeping.

Regression tests from a real run ("list of digital technology expos worldwide 2026"), which
accepted hundreds of rows that were category lookups ({id, name, slug}), form settings and
figures cut out of sentences ("costs $2,695"), while real events were typed as dates.
"""

from datetime import date
from unittest import TestCase

from vora.extraction.records import assess_record, field_kind
from vora.extraction.scoring import ObservationScorer
from vora.research.planning.provider import analyze_goal
from vora.shared.contracts import Observation

TODAY = date(2026, 9, 30)


class RecordShapeTests(TestCase):
    def test_a_lookup_entry_with_only_keys_is_not_a_record(self) -> None:
        # a category from a site's own taxonomy: an id, the name, and a slug that repeats the name
        verdict = assess_record({"id": "24", "name": "Apparel & Garments", "slug": "apparel-garments"})
        self.assertFalse(verdict.ok)

    def test_settings_and_flags_are_not_attributes(self) -> None:
        verdict = assess_record({"default": "false", "fields_enabled": "true", "fields_displayorder": "3",
                                 "fields_groupname": "Contact Details"})
        self.assertFalse(verdict.ok)

    def test_a_value_that_repeats_the_name_adds_nothing(self) -> None:
        self.assertFalse(assess_record({"name": "GITEX Global", "event": "GITEX Global"}).ok)

    def test_a_real_event_is_a_record(self) -> None:
        verdict = assess_record({"name": "GITEX Global 2026", "dates": "Oct 12-16, 2026", "url": "https://gitex.com"})
        self.assertTrue(verdict.ok, verdict.summary)
        self.assertEqual(verdict.title_field, "name")

    def test_a_real_identifier_still_counts_though_the_label_ends_in_id(self) -> None:
        verdict = assess_record({"title": "Notification on tariffs", "gazette_id": "CG-DL-E-12092026-268341",
                                 "issue_date": "12 Sep 2026"})
        self.assertTrue(verdict.ok, verdict.summary)

    def test_a_name_that_mentions_a_year_is_text_but_a_date_is_a_date(self) -> None:
        self.assertEqual(field_kind("name", "GITEX Global 2026"), "text")
        self.assertEqual(field_kind("name", "Oct 01 GITEX - Hanoi, Vietnam"), "text")
        for date_text in ("Mar 16-19, 2026", "12 September 2026", "2026-03-12", "Q3 FY2025", "FY 2024-25"):
            with self.subTest(value=date_text):
                self.assertEqual(field_kind("when", date_text), "date")


class SentenceFiguresTests(TestCase):
    def score(self, goal: str, method: str):
        plan = analyze_goal(goal, use_llm=False, today=TODAY)
        row = Observation(source_url="https://example.test/expos", method=method,
                          fields={"statement": "Cons: Expo floor is sales-heavy, and a full conference pass costs more",
                                  "value": "$2,695", "period": "2026"},
                          context="Digital technology expos 2026")
        return plan, ObservationScorer(plan, today=TODAY).score_all([row])

    def test_a_figure_in_a_sentence_is_not_an_answer_to_a_list_request(self) -> None:
        plan, (accepted, *_rest) = self.score("list of digital technology expos worldwide 2026", "undated_statement")
        self.assertEqual(plan.answer_shape, "either")
        self.assertEqual(accepted, [])

    def test_it_still_counts_when_the_request_asks_for_a_number(self) -> None:
        plan, _ = self.score("average price of expo tickets in 2026", "undated_statement")
        self.assertEqual(plan.answer_shape, "quantities")


class WindowedRecordTests(TestCase):
    def score(self, fields: dict, title: str = "Digital technology expos 2026 guide"):
        plan = analyze_goal("list of digital technology expos worldwide 2026", use_llm=False, today=TODAY)
        row = Observation(source_url="https://example.test/blog", method="network_json", fields=fields,
                          source_title=title, context=title)
        return ObservationScorer(plan, today=TODAY).score_all([row])

    def test_a_year_in_the_page_title_does_not_date_a_record(self) -> None:
        accepted, *_ = self.score({"fields_name": "firstname", "fields_label": "First name", "fields_type": "string",
                                   "fields_grouptitle": "Contact information", "fields_enabled": "True"})
        self.assertEqual(accepted, [])

    def test_a_record_with_its_own_date_is_accepted(self) -> None:
        accepted, *_ = self.score({"name": "GITEX Global 2026", "dates": "Oct 12-16, 2026", "location": "Dubai, UAE"})
        self.assertEqual(len(accepted), 1)
