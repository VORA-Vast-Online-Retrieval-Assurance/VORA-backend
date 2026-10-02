import io
import json
from datetime import date
from pathlib import Path
from types import MappingProxyType
from unittest import TestCase

from bs4 import BeautifulSoup
from openpyxl import Workbook

from vora.browser.contracts import ExecutionResult
from vora.extraction.files import FileParseError, file_extension, file_type, find_file_links, parse_file, relevance
from vora.extraction.parser import parse_page
from vora.extraction.requirements import build_requirements
from vora.extraction.scoring import ObservationScorer
from vora.shared.contracts import GoalPlan

TODAY = date(2026, 9, 29)
WINDOW = ("2022-01-01", "2026-12-31")
FIXTURE = Path(__file__).parent / "fixtures" / "crop_report.pdf"


def plan_for(goal: str) -> GoalPlan:
    fields = build_requirements(goal, today=TODAY)
    fields.pop("corrected_goal")
    return GoalPlan(normalized_goal=goal, **fields)


class ClassificationTests(TestCase):
    def test_extensions_and_types(self) -> None:
        self.assertEqual(file_extension("https://x.test/a/Report%202024.PDF"), "pdf")
        self.assertEqual(file_extension("https://x.test/export?format=csv"), "csv")
        self.assertIsNone(file_extension("https://x.test/page.html"))
        self.assertEqual(file_type("xlsx"), "spreadsheet")
        self.assertEqual(file_type("exe"), "program")
        self.assertEqual(file_type("zip"), "archive")

    def test_every_linked_file_is_found_and_ranked(self) -> None:
        soup = BeautifulSoup('<a href="/files/yield.xlsx">Yield tables 2024</a><a href="/setup.exe">Installer</a>'
                             '<a href="/about">About</a><iframe src="/docs/report.pdf"></iframe>', "html.parser")
        found = {item.extension: item for item in find_file_links(soup, "https://stats.test/page")}
        self.assertEqual(set(found), {"xlsx", "exe", "pdf"})
        self.assertTrue(found["xlsx"].extractable)
        self.assertFalse(found["exe"].extractable)
        self.assertGreater(relevance(found["xlsx"], ["crop", "yield"]), relevance(found["exe"], ["crop", "yield"]))


class ParsingTests(TestCase):
    def test_pdf_tables_and_text_use_the_page_extractor(self) -> None:
        parsed = parse_file(FIXTURE.read_bytes(), "pdf", url="https://stats.test/report.pdf", title="report.pdf",
                            window=WINDOW, places=[])
        tables = [item for item in parsed.observations if item.method == "pdf_table"]
        self.assertEqual(len(tables), 6)
        self.assertEqual(tables[0].context, "Table 1: Wheat yield by country, 2022-2024 (t/ha)")
        self.assertEqual(tables[0].context_kind, "caption")
        statement = next(item for item in parsed.observations if item.method == "pdf_text")
        self.assertEqual((statement.fields["value"], statement.fields["change"]), ("3.6", "2%"))
        accepted, _, _ = ObservationScorer(plan_for("wheat yield in the last 5 years"), today=TODAY).score_all(tables)
        self.assertEqual(len(accepted), 6)

    def test_spreadsheet_title_rows_year_columns_and_places(self) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Yields"
        for row in (["Table 4. Maize yield (t/ha)"], [], ["Country", 2021, 2022, 2023],
                    ["India", 3.1, 3.2, 3.3], ["Brazil", 5.5, 5.8, 6.0]):
            sheet.append(row)
        buffer = io.BytesIO()
        workbook.save(buffer)
        parsed = parse_file(buffer.getvalue(), "xlsx", url="https://stats.test/maize.xlsx", title="maize.xlsx",
                            window=WINDOW, places=["Brazil"])
        self.assertEqual([item.fields for item in parsed.observations],
                         [{"country": "Brazil", "period": "2023", "value": "6"},
                          {"country": "Brazil", "period": "2022", "value": "5.8"}])
        self.assertEqual(parsed.observations[0].context, "Table 4. Maize yield (t/ha) · Yields")
        accepted, _, _ = ObservationScorer(plan_for("maize yield since 2022"), today=TODAY).score_all(parsed.observations)
        self.assertEqual(len(accepted), 2)

    def test_json_records(self) -> None:
        payload = json.dumps({"meta": {}, "data": [{"country": "India", "year": 2024, "yield": 3.5},
                                                   {"country": "India", "year": 2019, "yield": 3.1}]}).encode()
        parsed = parse_file(payload, "json", url="https://stats.test/y.json", title="y.json", window=WINDOW, places=[])
        self.assertEqual([item.fields for item in parsed.observations],
                         [{"country": "India", "year": "2024", "yield": "3.5"}])

    def test_wrong_or_unsupported_content_is_refused(self) -> None:
        cases = [(b"<!doctype html><html></html>", "pdf", "web page"), (b"hello", "pdf", "not a PDF"),
                 (b"PK..", "docx", "not supported"), (b"MZ", "exe", "not supported"),
                 (b'{"a": 1}', "json", "not tabular")]
        for content, extension, message in cases:
            with self.subTest(extension=extension), self.assertRaisesRegex(FileParseError, message):
                parse_file(content, extension, url="https://x.test/f", title="f", window=WINDOW, places=[])


class SiteAdapterTests(TestCase):
    CARDEKHO = """<html><head><title>Electric Cars in India</title></head><body><main>
    <ul><li class="gsc_col-xs-12" data-price="1"><h3><a title="Tata Nexon EV">Tata Nexon EV</a></h3>
      <div class="price">₹12.49 - 17.19 Lakh</div><span class="brandName">Tata</span><span class="rangeText">465 km</span></li>
    <li class="gsc_col-xs-12" data-price="1"><h3>MG Windsor EV</h3><div class="price">₹14 - 16 Lakh</div></li></ul>
    </main></body></html>"""
    CARWALE = """<html><body><main><div data-testing-id="make-model-card"><h3 data-testing-id="model-name">Mahindra BE 6</h3>
    <span class="o-price">Rs. 18.90 - 26.90 Lakh</span></div></main></body></html>"""

    def render(self, url: str, html: str, adapters: bool = True):
        result = ExecutionResult(execution_id="x", requested_url=url, final_url=url, title="", html=html,
                                 status=200, elapsed_seconds=0.1, network_idle_reached=True,
                                 metadata=MappingProxyType({"fetched_at": "2026-09-29T08:00:00+00:00"}))
        return [item for item in parse_page(result, site_adapters=adapters).observations if item.method == "site_adapter"]

    def test_cardekho_listing_rows(self) -> None:
        rows = self.render("https://www.cardekho.com/electric-cars", self.CARDEKHO)
        first = rows[0].fields
        self.assertEqual((first["model"], first["price"], first["brand"], first["range"]),
                         ("Tata Nexon EV", "₹12.49 - 17.19 Lakh", "Tata", "465 km"))
        accepted, _, _ = ObservationScorer(plan_for("EV car prices in India this year"), today=TODAY).score_all(rows)
        self.assertEqual(len(accepted), 2)

    def test_carwale_listing_rows_and_switch(self) -> None:
        rows = self.render("https://www.carwale.com/electric-cars/", self.CARWALE)
        self.assertEqual(rows[0].fields, {"model": "Mahindra BE 6", "price": "Rs. 18.90 - 26.90 Lakh"})
        self.assertEqual(self.render("https://www.carwale.com/electric-cars/", self.CARWALE, adapters=False), [])
        self.assertEqual(self.render("https://other.test/cars", self.CARWALE), [])
