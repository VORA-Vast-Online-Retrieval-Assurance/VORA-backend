from datetime import date
from unittest import TestCase
from unittest.mock import patch

import httpx
from bs4 import BeautifulSoup

from vora.extraction.datasets import DatasetLink, find_dataset_links, parse_csv, rank_links
from vora.extraction.parser import dataset_observations
from vora.extraction.requirements import build_requirements
from vora.extraction.scoring import ObservationScorer
from vora.shared.contracts import GoalPlan
from vora.output import datasets as downloader

TODAY = date(2026, 9, 29)
PAGE = "https://ourworldindata.org/crop-yields"

OWID_LIKE_PAGE = """<html><body>
<figure class="explorer" data-explorer-src="https://ourworldindata.org/explorers/crop-yields?country=IND~USA&Crop=Wheat&Metric=Actual+yield&hideControls=false">
  <form class="ExplorerControlBar"><select><option>Almonds</option><option>Wheat</option></select></form>
</figure>
<a href="https://ourworldindata.org/grapher/key-crop-yields">Key crop yields</a>
<a href="https://ourworldindata.org/grapher/cereal-yield-vs-gdp-per-capita">Cereal yield vs GDP per capita</a>
<a href="https://ourworldindata.org/grapher/share-of-land-used-for-agriculture">Land use</a>
<a href="/downloads/yields.csv">Download all yields (CSV)</a>
<a href="https://example.org/about">About us</a>
</body></html>"""

WIDE_CSV = """Entity,Code,Year,Wheat,Rice,Soybeans
India,IND,2019,3.53,4.06,
India,IND,2023,3.52,4.32,1.17
India,IND,2024,3.56,4.31,
Kenya,KEN,2024,2.61,4.2,0.9
"""


def plan_for(goal: str, subject: list[str] | None = None) -> GoalPlan:
    fields = build_requirements(goal, today=TODAY)
    fields.pop("corrected_goal")
    if subject:
        fields["subject_terms"] = [*fields["subject_terms"], *subject]
    return GoalPlan(normalized_goal=goal, **fields)


class LinkDiscoveryTests(TestCase):
    def test_embedded_charts_and_data_files_are_found(self) -> None:
        links = find_dataset_links(BeautifulSoup(OWID_LIKE_PAGE, "html.parser"), PAGE)
        urls = [link.url for link in links]
        explorer = links[0]
        self.assertTrue(explorer.primary)
        self.assertTrue(explorer.url.startswith("https://ourworldindata.org/explorers/crop-yields.csv?"))
        self.assertIn("Crop=Wheat", explorer.url)
        self.assertNotIn("country=", explorer.url)  # full dataset, not the on-screen subset
        self.assertIn("https://ourworldindata.org/grapher/key-crop-yields.csv?csvType=full&useColumnShortNames=false", urls)
        self.assertIn("https://ourworldindata.org/downloads/yields.csv", urls)
        self.assertNotIn("https://example.org/about", urls)
        key = next(link for link in links if "key-crop-yields" in link.url)
        self.assertEqual(key.metadata_url, "https://ourworldindata.org/grapher/key-crop-yields.metadata.json")

    def test_ranking_prefers_datasets_about_the_request(self) -> None:
        links = find_dataset_links(BeautifulSoup(OWID_LIKE_PAGE, "html.parser"), PAGE)
        ranked = rank_links(links, ["crop", "yield"], 2)
        self.assertEqual(len(ranked), 2)
        self.assertTrue(all("yield" in link.url for link in ranked))
        self.assertNotIn("share-of-land", " ".join(link.url for link in rank_links(links, ["crop", "yield"], 10)))


class CsvTests(TestCase):
    def test_window_places_and_wide_measures(self) -> None:
        table = parse_csv(WIDE_CSV, window=("2022-01-01", "2026-12-31"), places=["India", "EV"])
        self.assertEqual(table.total, 4)
        self.assertEqual(table.dropped_out_of_window, 1)
        self.assertTrue(all(row["Entity"] == "India" for row in table.rows))
        self.assertIn({"Entity": "India", "Code": "IND", "Year": "2023", "series": "Soybeans", "value": "1.17"},
                      table.rows)
        self.assertEqual(table.rows[0]["Year"], "2024")  # latest first

    def test_single_measure_keeps_its_column_name(self) -> None:
        table = parse_csv("Entity,Year,Wheat yield\nIndia,2024,6.2031\n")
        self.assertEqual(table.rows, [{"Entity": "India", "Year": "2024", "Wheat yield": "6.2031"}])


class ScoringTests(TestCase):
    def test_dataset_rows_are_accepted_with_explicit_periods(self) -> None:
        link = DatasetLink("https://ourworldindata.org/grapher/key-crop-yields.csv", "Key crop yields", PAGE, "chart")
        plan = plan_for("Agricultural yeild in last 5 years", subject=["crop"])
        table = parse_csv(WIDE_CSV, window=(plan.time_scope.start, plan.time_scope.end))
        raw = dataset_observations(table.rows, link, title="Crop yields",
                                   context="Crop yields. Yields are measured in tonnes per hectare.",
                                   modified_at="2026-07-14", fetched_at="2026-09-29T00:00:00+00:00")
        accepted, partial, rejected = ObservationScorer(plan, today=TODAY).score_all(raw)
        self.assertEqual(len(accepted), len(raw))
        row = next(item for item in accepted if item.normalized.get("series") == "Wheat" and item.data_period == "2024"
                   and item.normalized.get("entity") == "India")
        self.assertEqual(row.normalized["yield"], "3.56")
        self.assertFalse(row.time_inferred)
        self.assertEqual(row.method, "linked_dataset")


class DownloadTests(TestCase):
    def test_download_reads_file_and_publisher_metadata(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith(".metadata.json"):
                return httpx.Response(200, json={"chart": {"title": "Wheat yields",
                                                           "subtitle": "Yields are measured in tonnes per hectare."},
                                                 "columns": {"Wheat": {"lastUpdated": "2026-07-14"}}})
            return httpx.Response(200, text="Entity,Year,Wheat yield\nIndia,2024,3.56\n",
                                  headers={"content-type": "text/csv"})

        link = DatasetLink("https://ourworldindata.org/grapher/wheat-yields.csv", "Wheat", PAGE, "chart",
                           "https://ourworldindata.org/grapher/wheat-yields.metadata.json")
        with patch.object(downloader, "ensure_public_url", lambda url: url):
            result = downloader.download(link, transport=httpx.MockTransport(handler))
        self.assertEqual(result.title, "Wheat yields")
        self.assertEqual(result.context, "Wheat yields. Yields are measured in tonnes per hectare")
        self.assertEqual(result.modified_at, "2026-07-14")
        self.assertIn("India,2024,3.56", result.text)

    def test_oversized_and_failed_downloads_raise(self) -> None:
        link = DatasetLink("https://data.test/big.csv", "Big", PAGE, "file")
        big = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * (downloader.MAX_BYTES + 1)))
        missing = httpx.MockTransport(lambda request: httpx.Response(404, text="nope"))
        with patch.object(downloader, "ensure_public_url", lambda url: url):
            with self.assertRaises(downloader.DatasetTooLarge):
                downloader.download(link, transport=big)
            with self.assertRaises(downloader.DatasetError):
                downloader.download(link, transport=missing)
