from datetime import date
from types import MappingProxyType
from unittest import TestCase

from vora.browser.contracts import ExecutionResult
from vora.extraction.parser import parse_rendered_page, unpivot_wide
from vora.extraction.requirements import build_requirements
from vora.extraction.scoring import ObservationScorer, classify_observations
from vora.shared.contracts import GoalPlan, Observation

TODAY = date(2026, 9, 29)


def rendered(html: str, url: str = "https://example.test/page", title: str = "",
             headers: dict | None = None) -> ExecutionResult:
    return ExecutionResult(
        execution_id="test", requested_url=url, final_url=url, title=title, html=html, status=200,
        elapsed_seconds=0.1, network_idle_reached=True,
        metadata=MappingProxyType({"fetched_at": "2026-09-29T08:00:00+00:00",
                                   "response_headers": MappingProxyType(headers or {})}),
    )


def plan_for(goal: str) -> GoalPlan:
    fields = build_requirements(goal, today=TODAY)
    fields.pop("corrected_goal")
    return GoalPlan(normalized_goal=goal, **fields)


BATTERY_PAGE = """
<html><head><title>EV Battery Price Chart: 2010-2025 Cost Trend</title>
<meta property="article:modified_time" content="2026-07-29T13:25:48+00:00">
<meta property="article:published_time" content="2026-07-29T13:25:47+00:00"></head>
<body><nav><ul class="menu"><li class="menu-item"><a href="/about">About</a></li>
<li class="menu-item"><a href="/news">News</a></li></ul></nav>
<main><h2>Battery pack prices by year</h2>
<table><tr><th>Year</th><th>Average Pack Price</th><th>Change</th></tr>
<tr><td>2010</td><td>above $1,100/kWh</td><td>–</td></tr>
<tr><td>2019</td><td>$161/kWh</td><td>down 13%</td></tr>
<tr><td>2024</td><td>$115/kWh</td><td>down 20%</td></tr></table>
<p>Pack prices fell to $108/kWh in 2025, according to the annual survey.</p></main>
<footer><div class="footer-links"><a href="/privacy">Privacy</a></div></footer></body></html>
"""


class ParsingTests(TestCase):
    def test_page_provenance_is_attached_to_every_observation(self) -> None:
        raw = parse_rendered_page(rendered(BATTERY_PAGE, url="https://www.evdata.test/chart"))
        row = next(item for item in raw if item.fields.get("year") == "2024")
        self.assertEqual(row.modified_at, "2026-07-29")
        self.assertEqual(row.published_at, "2026-07-29")
        self.assertEqual(row.source_domain, "evdata.test")
        self.assertIn("Battery Price Chart", row.source_title)
        self.assertEqual(row.context, "Battery pack prices by year")
        self.assertEqual(row.method, "html_table")
        self.assertGreater(row.extraction_confidence, 0.8)

    def test_navigation_and_footer_do_not_become_observations(self) -> None:
        raw = parse_rendered_page(rendered(BATTERY_PAGE))
        texts = " ".join(" ".join(item.fields.values()) for item in raw if item.content_role == "data")
        self.assertNotIn("About", texts)
        self.assertNotIn("Privacy", texts)

    def test_prose_statements_are_extracted(self) -> None:
        raw = parse_rendered_page(rendered(BATTERY_PAGE))
        statement = next(item for item in raw if item.method == "text_statement")
        self.assertEqual(statement.fields["value"], "$108/kWh")
        self.assertEqual(statement.fields["period"], "2025")

    def test_prose_distinguishes_levels_changes_and_bounds(self) -> None:
        html = """<main>
        <p>Battery pack prices fell to $108/kWh in 2025, a record low and down 8% from 2024.</p>
        <p>In 2023 a mid-size EV cost $3,000 less than a comparable petrol car in most markets.</p>
        <p>Used EVs under $20,000 were hard to find throughout 2026 across every dealer group.</p></main>"""
        raw = parse_rendered_page(rendered(html))
        statements = {item.fields["statement"][:20]: item.fields for item in raw if item.method == "text_statement"}
        self.assertEqual(statements["Battery pack prices "]["value"], "$108/kWh")
        self.assertEqual(statements["Battery pack prices "]["change"], "8%")
        self.assertEqual(statements["In 2023 a mid-size E"]["change"], "$3,000")
        self.assertNotIn("value", statements["In 2023 a mid-size E"])
        self.assertNotIn("Used EVs under $20,0", statements)  # a bound only: not a statement of value

    def test_wide_period_tables_are_unpivoted_with_row_labels(self) -> None:
        html = """<table><tr><th></th><th>1/2024</th><th>2/2024</th><th>3/2024</th></tr>
        <tr><td>Electric</td><td>$55,353</td><td>$53,707</td><td>$54,021</td></tr>
        <tr><td>All vehicles</td><td>$47,401</td><td>$47,244</td><td>$47,218</td></tr></table>"""
        raw = parse_rendered_page(rendered(html))
        self.assertEqual(len(raw), 6)
        self.assertEqual(raw[0].fields, {"series": "Electric", "period": "1/2024", "value": "$55,353"})

    def test_legacy_wide_observation_can_be_repaired(self) -> None:
        legacy = Observation(source_url="https://example.test", method="html_table",
                             fields={"1_2020": "$54,669", "2_2020": "$56,326", "3_2020": "$56,059"})
        rows = unpivot_wide(legacy)
        self.assertEqual([row.fields["period"] for row in rows], ["2020-01", "2020-02", "2020-03"])
        self.assertEqual(len({row.id for row in rows}), 3)

    def test_property_sheet_collapses_to_one_observation(self) -> None:
        html = """<table><tr><th>Field</th><th>Value</th></tr>
        <tr><td>Last Updated</td><td>July 14, 2026</td></tr><tr><td>Created</td><td>March 14, 2023</td></tr></table>"""
        raw = parse_rendered_page(rendered(html))
        self.assertEqual(len(raw), 1)
        self.assertEqual(raw[0].content_role, "metadata")

    def test_challenge_page_is_marked_before_scoring(self) -> None:
        html = "<html><head><title>Just a moment...</title></head><body>Performing security verification</body></html>"
        raw = parse_rendered_page(rendered(html))
        self.assertEqual([item.content_role for item in raw], ["challenge"])

    def test_sparse_page_content_is_preserved_before_classification(self) -> None:
        raw = parse_rendered_page(rendered("<main>Only one useful value</main>"))
        self.assertEqual(len(raw), 1)
        self.assertEqual(raw[0].status, "raw")
        self.assertEqual(raw[0].fields["text"], "Only one useful value")


class ScoringTests(TestCase):
    def score(self, goal: str, rows: list[Observation]):
        return ObservationScorer(plan_for(goal), today=TODAY).score_all(rows)

    def test_partial_observation_is_not_rejected_for_missing_optional_dimensions(self) -> None:
        plan = plan_for("EV car prices over the last 10 years")
        plan.concepts += [item.model_copy(update={"name": name, "role": "optional", "kind": "dimension",
                                                  "aliases": []})
                          for item, name in zip([plan.concepts[0]] * 3, ["make", "model", "trim_level"])]
        row = Observation(source_url="https://evdata.test/chart", method="html_table",
                          source_title="EV Battery Price Chart",
                          fields={"year": "2024", "average_pack_price": "$115/kWh"})
        accepted, partial, rejected = ObservationScorer(plan, today=TODAY).score_all([row])
        self.assertEqual(len(accepted), 1)
        result = accepted[0]
        self.assertEqual(result.data_period, "2024")
        self.assertFalse(result.time_inferred)
        self.assertEqual(result.normalized["price"], "$115/kWh")
        self.assertEqual(result.normalized["series"], "average pack price")
        self.assertGreater(result.concept_matches["price"]["credit"], 0.9)

    def test_explicit_historical_year_beats_page_modified_date(self) -> None:
        row = Observation(source_url="https://example.test", method="html_table", source_title="EV prices",
                          modified_at="2026-05-01", fields={"year": "2019", "price": "$40,000"})
        accepted, _, _ = self.score("EV prices over the last 10 years", [row])
        self.assertEqual(accepted[0].data_period, "2019")
        self.assertFalse(accepted[0].time_inferred)
        self.assertEqual(accepted[0].time_basis, "row_field")

    def test_snapshot_period_is_inferred_and_marked_uncertain(self) -> None:
        row = Observation(source_url="https://shop.test/gpus", method="repeated_region",
                          source_title="Graphics cards", modified_at="2026-09-01",
                          fields={"name": "GPU X 16GB", "price": "$599"})
        accepted, partial, _ = self.score("GPU prices this year", [row])
        result = (accepted or partial)[0]
        self.assertEqual(result.data_period, "2026")
        self.assertTrue(result.time_inferred)
        self.assertLess(result.temporal_confidence, 0.5)

    def test_out_of_range_rows_are_kept_as_partial_not_accepted(self) -> None:
        row = Observation(source_url="https://evdata.test", method="html_table", source_title="EV battery prices",
                          fields={"year": "2010", "average_pack_price": "$1,100/kWh"})
        accepted, partial, _ = self.score("EV prices over the last 10 years", [row])
        self.assertFalse(accepted)
        self.assertEqual(partial[0].tier, "partial")
        self.assertTrue(any("outside" in reason for reason in partial[0].reasons))

    def test_noise_is_rejected_without_semantic_scoring(self) -> None:
        rows = [
            Observation(source_url="https://example.test", method="page_summary",
                        fields={"title": "Just a moment...", "text": "Checking your browser before accessing"}),
            Observation(source_url="https://example.test", method="repeated_region",
                        fields={"url": "/about", "text": "About"}),
        ]
        accepted, partial, rejected = self.score("EV prices", rows)
        self.assertEqual((len(accepted), len(partial), len(rejected)), (0, 0, 2))
        self.assertEqual({item.tier for item in rejected}, {"noise"})

    def test_domain_independent_scoring(self) -> None:
        cases = [
            ("wheat yield in the last 5 years", {"year": "2023", "yield_t_ha": "3.6"}, "Wheat yields"),
            ("unemployment rate since 2020", {"period": "2024 Q2", "unemployment_rate": "4.1%"}, "Unemployment"),
            ("semiconductor shipments 2022-2024", {"year": "2023", "shipments_bn_units": "1.1"},
             "Semiconductor market"),
            ("company revenue since 2019", {"fiscal_year": "FY2023", "net_revenue": "$211.9 bn"},
             "Company annual report"),
        ]
        for goal, fields, title in cases:
            with self.subTest(goal=goal):
                row = Observation(source_url="https://data.test", method="html_table", source_title=title,
                                  fields=fields)
                accepted, partial, _ = self.score(goal, [row])
                self.assertEqual(len(accepted), 1, (partial[0].reasons if partial else None))

    def test_irrelevant_rows_are_not_accepted(self) -> None:
        row = Observation(source_url="https://recipes.test", method="html_table", source_title="Cake recipes",
                          fields={"year": "2024", "price": "$4"})
        accepted, _, _ = self.score("EV car prices over the last 10 years", [row])
        self.assertFalse(accepted)

    def test_classify_wrapper_on_parsed_page(self) -> None:
        html = "<table><tr><th>Year</th><th>Price</th></tr><tr><td>2020</td><td>10</td></tr></table>"
        raw = parse_rendered_page(rendered(html, title="Prices"))
        accepted, partial, rejected = classify_observations(raw, GoalPlan(
            normalized_goal="prices in 2020", required_fields=["year", "price"],
            year_start=2020, year_end=2020,
        ), today=TODAY)
        self.assertEqual(len(raw), 1)
        self.assertEqual(len(accepted), 1)
        self.assertFalse(partial or rejected)

    # Regressions found by inspecting a live EV run.
    def test_prose_mentioning_money_is_not_a_price_value(self) -> None:
        teaser = Observation(source_url="https://cars.test", method="repeated_region", source_title="EV prices",
                             fields={"text": "Used Cars Under $20,000 Are Getting Much Harder To Find for EV buyers",
                                     "url": "/study"})
        accepted, _, _ = self.score("EV car prices over the last 10 years", [teaser])
        self.assertFalse(accepted)

    def test_statement_value_not_sentence_becomes_the_measure(self) -> None:
        row = Observation(source_url="https://cars.test", method="text_statement", source_title="Used EV prices",
                          fields={"statement": "A 2023 Tesla Model 3 EV sells at $27,500, compared with rivals.",
                                  "value": "$27,500", "period": "2023"})
        accepted, _, _ = self.score("EV car prices over the last 10 years", [row])
        self.assertEqual(accepted[0].normalized["price"], "$27,500")

    def test_incompatible_units_do_not_satisfy_a_monetary_measure(self) -> None:
        rows = [
            Observation(source_url="https://cars.test", method="text_statement", source_title="EV market",
                        fields={"statement": "Used EV sales rose 14.7% year over year in August 2026.",
                                "value": "14.7%", "period": "2026-08"}),
            Observation(source_url="https://cars.test", method="text_statement", source_title="EV market",
                        fields={"statement": "The study analysed 1.8 million used cars and their listing prices in 2025.",
                                "value": "1.8 million", "period": "2025"}),
        ]
        accepted, _, _ = self.score("EV car prices over the last 10 years", rows)
        self.assertFalse(accepted)

    def test_spanning_title_row_becomes_caption_with_explicit_period(self) -> None:
        html = """<table><tr><th colspan="3">Top used EVs with the largest price increases, June 2026</th></tr>
        <tr><th>Model</th><th>Average price</th><th>Change</th></tr>
        <tr><td>Hatchback EV</td><td>$32,395</td><td>-1.3%</td></tr></table>"""
        raw = parse_rendered_page(rendered(html, title="Used EV prices"))
        self.assertEqual(raw[0].fields, {"model": "Hatchback EV", "average_price": "$32,395", "change": "-1.3%"})
        self.assertEqual(raw[0].context_kind, "caption")
        accepted, _, _ = self.score("EV car prices over the last 10 years", raw)
        self.assertEqual(accepted[0].data_period, "2026-06")
        self.assertFalse(accepted[0].time_inferred)

    def test_measure_attributed_to_another_noun_is_not_accepted(self) -> None:
        def statement(text: str, value: str) -> Observation:
            return Observation(source_url="https://iea.test/outlook", method="text_statement",
                               source_title="Global EV Outlook", context="Electric car markets",
                               fields={"statement": text, "value": value, "period": "2025"})
        gasoline = statement("Furthermore, gasoline prices are much higher than in neighbouring countries at USD 2 per litre in 2025.", "USD 2")
        bev = statement("BEV pack prices came in at $99/kWh in 2025, below $100/kWh for the second year.", "$99/kWh")
        accepted, partial, _ = self.score("EV car prices over the last 10 years", [gasoline, bev])
        self.assertEqual([item.fields["value"] for item in accepted], ["$99/kWh"])
        self.assertTrue(any("refers to something other" in reason for reason in partial[0].reasons))

    def test_bounds_in_titles_are_not_values(self) -> None:
        card = Observation(source_url="https://cars.test", method="repeated_region", source_title="Used EV guide",
                           fields={"name": "Best Used EVs Under $25,000", "url": "/best-used-evs"})
        accepted, _, _ = self.score("EV car prices over the last 10 years", [card])
        self.assertFalse(accepted)

    # Regressions found by inspecting a live agriculture run.
    def test_production_statement_is_not_accepted_as_yield(self) -> None:
        row = Observation(source_url="https://stats.test", method="text_statement",
                          source_title="Agricultural production statistics",
                          fields={"statement": "World meat production reached 374 million tonnes in 2024 in agricultural markets.",
                                  "value": "374 million tonnes", "period": "2024"})
        accepted, partial, _ = self.score("Agricultural yeild in last 5 years", [row])
        self.assertFalse(accepted)
        self.assertEqual(partial[0].tier, "partial")  # relevant, kept for review
        self.assertNotIn("yield", partial[0].normalized)

    def test_generic_column_takes_meaning_from_its_caption(self) -> None:
        html = """<table><caption>Wheat yield, tonnes per hectare</caption>
        <tr><td>2023</td><td>3.6</td></tr><tr><td>2024</td><td>3.8</td></tr></table>"""
        raw = parse_rendered_page(rendered(html, title="Wheat statistics"))
        accepted, _, _ = self.score("wheat yield in the last 5 years", raw)
        self.assertEqual(sorted(item.data_period for item in accepted), ["2023", "2024"])
        self.assertEqual(accepted[0].concept_matches["yield"]["via"], "context")

    def test_parsed_battery_page_end_to_end(self) -> None:
        raw = parse_rendered_page(rendered(BATTERY_PAGE, url="https://evdata.test/chart"))
        accepted, partial, rejected = self.score("EV car prices over the last 10 years", raw)
        periods = sorted(item.data_period for item in accepted)
        self.assertEqual(periods, ["2019", "2024", "2025"])
        self.assertIn("2010", [item.data_period for item in partial])
