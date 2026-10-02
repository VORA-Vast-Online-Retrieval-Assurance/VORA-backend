from datetime import date
from unittest import TestCase

from vora.extraction.temporal import find_periods, parse_datetime, parse_period, resolve_observation_period, resolve_time_window

TODAY = date(2026, 9, 29)


class RelativeWindowTests(TestCase):
    def test_last_ten_years_is_exactly_ten_calendar_years(self) -> None:
        window = resolve_time_window("EV car prices over the last 10 years", TODAY)
        self.assertEqual(len(window.periods), 10)
        self.assertEqual(window.periods[0], "2017")
        self.assertEqual(window.periods[-1], "2026")
        self.assertEqual((window.start, window.end), (date(2017, 1, 1), date(2026, 12, 31)))

    def test_every_relative_phrasing_produces_n_periods(self) -> None:
        for phrase in ("last 10 years", "past 10 years", "previous ten years", "prior 10 yrs"):
            with self.subTest(phrase=phrase):
                self.assertEqual(len(resolve_time_window(phrase, TODAY).periods), 10)

    def test_complete_years_end_at_previous_year(self) -> None:
        window = resolve_time_window("sales for the last 5 complete years", TODAY)
        self.assertEqual(window.periods, ("2021", "2022", "2023", "2024", "2025"))

    def test_months_quarters_days_and_decade(self) -> None:
        months = resolve_time_window("GPU prices in the past 6 months", TODAY)
        self.assertEqual(months.periods, ("2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"))
        quarters = resolve_time_window("revenue for the last 4 quarters", TODAY)
        self.assertEqual(quarters.periods, ("2025-Q4", "2026-Q1", "2026-Q2", "2026-Q3"))
        days = resolve_time_window("energy prices past 30 days", TODAY)
        self.assertEqual(len(days.periods), 30)
        self.assertEqual(days.end, TODAY)
        decade = resolve_time_window("unemployment over the last decade", TODAY)
        self.assertEqual(len(decade.periods), 10)

    def test_explicit_and_open_ended_ranges(self) -> None:
        self.assertEqual(resolve_time_window("prices from 2015 to 2020", TODAY).periods,
                         tuple(str(year) for year in range(2015, 2021)))
        self.assertEqual(resolve_time_window("population since 2019", TODAY).periods[-1], "2026")
        self.assertEqual(resolve_time_window("prices last year", TODAY).periods, ("2025",))
        self.assertIsNone(resolve_time_window("current EV prices", TODAY))

    def test_window_is_relative_to_supplied_date(self) -> None:
        self.assertEqual(resolve_time_window("last 3 years", date(2030, 1, 5)).periods,
                         ("2028", "2029", "2030"))


class PeriodParsingTests(TestCase):
    def test_cell_formats(self) -> None:
        expected = {
            "2024": "2024", "1/2020": "2020-01", "1_2020": "2020-01", "Q2 2025": "2025-Q2",
            "2025 Q3": "2025-Q3", "March 2026": "2026-03", "FY2024": "FY2024", "H1 2024": "2024-H1",
            "July 14, 2026": "2026-07-14", "31/12/2025": "2025-12-31", "2019-20": "2019-20",
        }
        for text, label in expected.items():
            with self.subTest(text=text):
                self.assertEqual(parse_period(text, TODAY).label, label)

    def test_amounts_are_not_years(self) -> None:
        self.assertIsNone(parse_period("$2,024"))
        self.assertIsNone(parse_period("12.5"))
        self.assertIsNone(parse_period("6.2031"))   # a yield in t/ha, not June 2031
        self.assertIsNone(parse_period("1.2025"))
        self.assertEqual([item.label for item in find_periods("fees of $2,024 rose")], [])

    def test_provenance_timestamps(self) -> None:
        self.assertEqual(parse_datetime("2026-07-29T13:25:47+00:00").date(), date(2026, 7, 29))
        self.assertEqual(parse_datetime("Tue, 15 Nov 1994 08:12:31 GMT").year, 1994)
        self.assertEqual(parse_datetime("July 14, 2026, 10:23 PM (UTC+05:30)").date(), date(2026, 7, 14))


class ObservationPeriodTests(TestCase):
    def test_explicit_row_year_beats_page_modification_date(self) -> None:
        resolution = resolve_observation_period(
            {"year": "2019", "price": "$40,000"}, ["year"],
            modified_at="2026-03-01", published_at="2025-12-01", today=TODAY,
        )
        self.assertEqual(resolution.period.label, "2019")
        self.assertFalse(resolution.inferred)
        self.assertEqual(resolution.basis, "row_field")

    def test_table_caption_period_is_explicit(self) -> None:
        resolution = resolve_observation_period(
            {"model": "Compact", "price": "$31,000"}, [], context="Average prices in 2023",
            context_kind="caption", modified_at="2026-01-01", today=TODAY,
        )
        self.assertEqual(resolution.period.label, "2023")
        self.assertFalse(resolution.inferred)

    def test_snapshot_period_is_inferred_with_low_confidence(self) -> None:
        resolution = resolve_observation_period(
            {"model": "Compact", "price": "$31,000"}, [], modified_at="2026-08-20", today=TODAY,
        )
        self.assertEqual(resolution.period.label, "2026")
        self.assertTrue(resolution.inferred)
        self.assertEqual(resolution.basis, "modified_at")
        self.assertLess(resolution.confidence, 0.5)

    def test_fetch_date_is_the_weakest_basis(self) -> None:
        resolution = resolve_observation_period(
            {"model": "Compact", "price": "$31,000"}, [], fetched_at="2026-09-29T10:00:00+00:00",
            today=TODAY,
        )
        self.assertEqual(resolution.basis, "fetched_at")
        self.assertLessEqual(resolution.confidence, 0.25)

    def test_no_inference_for_multi_period_pages_or_rows_without_values(self) -> None:
        series_page = resolve_observation_period(
            {"model": "Compact", "price": "$31,000"}, [], page_title="Price chart 2010-2025",
            modified_at="2026-08-20", today=TODAY,
        )
        self.assertIsNone(series_page.period)
        no_value = resolve_observation_period({"name": "About us"}, [], modified_at="2026-08-20",
                                              has_measure=False, today=TODAY)
        self.assertIsNone(no_value.period)
