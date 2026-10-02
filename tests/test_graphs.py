import os

# These tests use made-up sites and a stub planner: no probing, no model calls.
os.environ["VORA_PROBE_SOURCES"] = "false"
os.environ["VORA_RESOLVER_SITES"] = "0"
os.environ["VORA_SOURCE_CACHE"] = "false"
os.environ["VORA_QUERY_CACHE_MINUTES"] = "0"

import os
import dataclasses
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

from vora.api import application
from vora.shared.contracts import GoalPlan, Observation, ResearchSnapshot
from vora.research.coordinator import ResearchCoordinator
from vora.output.graphs import graph_data, graph_parameters, to_number
from vora.storage.repository import Repository



def row(values: dict[str, str], domain: str = "stats.test") -> Observation:
    return Observation(source_url=f"https://{domain}/page", method="html_table", fields=values,
                       normalized=values, status="accepted")


WINS = [row({"period": "2023", "entity": "Chennai", "value": "10"}),
        row({"period": "2024", "entity": "Chennai", "value": "8"}),
        row({"period": "2023", "entity": "Gujarat", "value": "11"}),
        row({"period": "2024", "entity": "Gujarat", "value": "7"})]
CITIES = [row({"period": "2026", "geography": "Tokyo", "population": "13.96 million"}),
          row({"period": "2026", "geography": "Osaka", "population": "2,750,000"}),
          row({"period": "2026", "geography": "Yokohama", "population": "3,770,000"})]


class GraphTests(TestCase):
    def test_to_number_matches_the_interface(self) -> None:
        self.assertEqual(to_number("$54,669"), 54669)
        self.assertEqual(to_number("Rs. 14.49 Lakh"), 1449000)
        self.assertEqual(to_number("13.96 million"), 13960000)
        self.assertEqual(to_number("down 20%"), -20)
        self.assertIsNone(to_number("n/a"))

    def test_every_numeric_parameter_is_listed(self) -> None:
        listing = graph_parameters([*WINS, *CITIES])
        by_name = {item["name"]: item for item in listing["parameters"]}
        self.assertEqual(set(by_name), {"value", "population"})
        self.assertEqual(by_name["value"]["x_kind"], "period")
        self.assertEqual(by_name["population"]["x_kind"], "category")
        self.assertEqual((by_name["value"]["period_min"], by_name["value"]["period_max"]), ("2023", "2024"))

    def test_time_parameters_become_one_line_per_group(self) -> None:
        chart = graph_data(WINS)["charts"][0]
        self.assertEqual((chart["type"], chart["x"], chart["group"]), ("line", "period", "entity"))
        self.assertEqual({item["name"]: [point["value"] for point in item["points"]] for item in chart["series"]},
                         {"Chennai": [10, 8], "Gujarat": [11, 7]})
        self.assertTrue(chart["series"][0]["points"][0]["observation_id"])

    def test_parameters_without_periods_become_bars(self) -> None:
        chart = graph_data(CITIES)["charts"][0]
        self.assertEqual((chart["type"], chart["group"]), ("bar", "geography"))
        self.assertEqual([point["x"] for point in chart["series"][0]["points"]], ["Tokyo", "Yokohama", "Osaka"])

    def test_identifier_columns_are_not_parameters_or_groups(self) -> None:
        rows = [row({"period": str(2020 + index), "rank": str(index + 1), "purposeid": f"P{index}",
                     "value": str(100 + index)}) for index in range(3)]
        listing = graph_parameters(rows)
        self.assertEqual([item["name"] for item in listing["parameters"]], ["value"])
        self.assertNotIn("purposeid", [item["name"] for item in listing["dimensions"]])
        self.assertIsNone(graph_data(rows)["charts"][0]["group"])

    def test_selection_range_and_series_filters(self) -> None:
        data = graph_data([*WINS, *CITIES], ["population"])
        self.assertEqual(data["selected"], ["population"])
        self.assertEqual(len(data["charts"]), 1)
        only_2024 = graph_data(WINS, start="2024", end="2024")["charts"][0]
        self.assertEqual(only_2024["type"], "bar")  # a single period left
        chennai = graph_data(WINS, series=["Chennai"])["charts"][0]
        self.assertEqual([item["name"] for item in chennai["series"]], ["Chennai"])


class GraphApiTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repository = Repository(Path(self.temporary.name) / "graphs.db")
        for name, value in (("repository", self.repository),
                            ("coordinator", ResearchCoordinator(self.repository)),
                            ("settings", dataclasses.replace(application.settings, auth="", api_key=None))):
            patcher = patch.object(application, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(application.app)

    def test_graph_endpoints(self) -> None:
        instance = self.repository.create_instance("Wins", "IPL wins")
        self.repository.save_snapshot(instance["id"], ResearchSnapshot(
            plan=GoalPlan(normalized_goal="IPL wins"), accepted=[*WINS, *CITIES]))
        base = f"/api/v1/instances/{instance['id']}/graph"
        listing = self.client.get(f"{base}/parameters").json()
        self.assertEqual({item["name"] for item in listing["parameters"]}, {"value", "population"})
        everything = self.client.get(base).json()
        self.assertEqual(len(everything["charts"]), 2)
        chosen = self.client.get(base, params={"parameters": "value", "from": "2024"}).json()
        self.assertEqual([chart["parameter"] for chart in chosen["charts"]], ["value"])
        self.assertEqual(self.client.get("/api/v1/instances/missing/graph").status_code, 404)


class TextColumnTests(TestCase):
    def test_a_text_column_with_a_few_figures_is_not_a_parameter(self) -> None:
        records = [Observation(source_url="https://egazette.gov.in/", method="recipe",
                               fields={"subject": text, "issue_date": "30-Sep-2026"})
                   for text in ["Tariff value notification", "Section 20 of 2026 amendment", "AGN 2030 rules",
                                "Appointment of Joint Secretary", "Rules for spectrum sharing", "CDSE Exam"]]
        self.assertNotIn("subject", [item["name"] for item in graph_parameters(records)["parameters"]])
