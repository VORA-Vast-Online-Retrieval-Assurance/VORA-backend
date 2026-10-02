"""The pipeline must not favour any kind of request.

Every topic below is planned without a language model and scored the same
way; a table or sentence that plainly answers the request must be accepted,
whatever the subject.
"""

import ast
import re
from datetime import date
from pathlib import Path
from types import MappingProxyType
from unittest import TestCase

from vora.browser.contracts import ExecutionResult
from vora.extraction.parser import parse_rendered_page
from vora.extraction.scoring import ObservationScorer
from vora.research.planning.provider import analyze_goal

TODAY = date(2026, 9, 29)
ROOT = Path(__file__).resolve().parents[1]


def page_html(title: str, header: list[str], rows: list[list[str]], prose: str = "") -> str:
    head = "".join(f"<th>{cell}</th>" for cell in header)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    table = f"<table><tr>{head}</tr>{body}</table>" if header else ""
    return (f"<html><head><title>{title}</title></head><body><main><h1>{title}</h1>"
            f"{table}<p>{prose}</p></main></body></html>")


def score(goal: str, html: str):
    plan = analyze_goal(goal, use_llm=False, today=TODAY)
    title = re.search(r"<title>(.*?)</title>", html).group(1)
    result = ExecutionResult(
        execution_id="bias", requested_url="https://stats.test/page", final_url="https://stats.test/page",
        title=title, html=html, status=200, elapsed_seconds=0.1, network_idle_reached=True,
        metadata=MappingProxyType({"fetched_at": "2026-09-29T08:00:00+00:00"}),
    )
    return plan, ObservationScorer(plan, today=TODAY).score_all(parse_rendered_page(result))


TOPICS = {
    "number of hospitals per state in India": (
        "Number of hospitals by state in India", ["State", "Hospitals"],
        [["Kerala", "1,280"], ["Tamil Nadu", "1,560"], ["Bihar", "780"]]),
    "top programming languages popularity 2024": (
        "Programming language popularity 2024", ["Rank", "Language", "Popularity"],
        [["1", "Python", "28.1%"], ["2", "JavaScript", "20.4%"], ["3", "Java", "15.2%"]]),
    "IPL cricket team wins by season": (
        "IPL team wins by season", ["Season", "Team", "Wins"],
        [["2023", "Chennai Super Kings", "10"], ["2024", "Kolkata Knight Riders", "11"]]),
    "population of cities in Japan": (
        "Population of cities in Japan", ["City", "Population"],
        [["Tokyo", "13,960,000"], ["Osaka", "2,750,000"]]),
    "rainfall by month in Kerala": (
        "Kerala monthly rainfall", ["Month", "Rainfall (mm)"],
        [["June 2024", "648.3"], ["July 2024", "653.5"]]),
    "EV car prices in India": (
        "EV car prices in India", ["Model", "Price"],
        [["Tata Nexon EV", "Rs. 14.49 Lakh"], ["MG ZS EV", "Rs. 18.98 Lakh"]]),
    "crop yield in last 5 years": (
        "Crop yield statistics", ["Year", "Yield (t/ha)"],
        [["2023", "4.1"], ["2024", "4.0"], ["2025", "4.2"]]),
    "GDP by country 2024": (
        "GDP by country 2024", ["Country", "GDP (USD billion)"],
        [["India", "3,900"], ["Japan", "4,100"]]),
}


class BiasTests(TestCase):
    def test_every_kind_of_request_gets_accepted_rows(self) -> None:
        for goal, (title, header, rows) in TOPICS.items():
            with self.subTest(goal=goal):
                plan, (accepted, partial, _) = score(goal, page_html(title, header, rows))
                self.assertEqual(plan.planner, "heuristic")
                self.assertEqual(len(accepted), len(rows), [item.reasons for item in partial])

    def test_identifier_and_rank_columns_are_not_the_measure(self) -> None:
        html = page_html("Hospitals in Kerala", ["Rank", "Hospital ID", "Hospital name"],
                         [["1", "1045", "General Hospital"], ["2", "2210", "Medical College"]])
        plan, (accepted, _, _) = score("number of hospitals in Kerala", html)
        self.assertEqual(plan.subject_heads, ["hospital"])
        self.assertEqual(accepted, [])

    def test_counts_in_prose_and_engagement_counters(self) -> None:
        prose = ("Kerala has 1,280 hospitals across its districts. "
                 "This page has 12,400 views and 3,100 comments from readers.")
        _, (accepted, partial, rejected) = score("number of hospitals in Kerala",
                                                 page_html("Kerala health facilities", [], [], prose))
        values = [item.fields.get("value") for item in [*accepted, *partial, *rejected]]
        self.assertIn("1,280 hospitals", values)
        self.assertFalse(any("views" in str(value) or "comments" in str(value) for value in values))
        statement = next(item for item in accepted if item.fields.get("value") == "1,280 hospitals")
        self.assertEqual(statement.method, "undated_statement")
        self.assertTrue(statement.time_inferred)

    def test_undated_sentences_must_name_what_is_counted(self) -> None:
        prose = ("With a total of 137,517 hectares available, the state plans new facilities. "
                 "Kerala has 1,280 hospitals across its districts.")
        _, (accepted, partial, _) = score("number of hospitals in Kerala",
                                          page_html("Kerala health facilities", [], [], prose))
        self.assertEqual([item.fields["value"] for item in accepted], ["1,280 hospitals"])
        hectares = next(item for item in partial if item.fields.get("value") == "137,517 hectares")
        self.assertIn("An undated sentence must name what is counted", hectares.reasons)

    def test_a_year_in_the_column_name_is_the_period(self) -> None:
        html = page_html("States of India by population", ["State", "2011 census population", "Growth 2001-2011"],
                         [["Uttar Pradesh", "199,812,341", "20.2%"], ["Gujarat", "60,439,692", "19.3%"]])
        _, (accepted, _, _) = score("population by state in India", html)
        self.assertEqual({item.data_period for item in accepted}, {"2011"})
        self.assertEqual({item.time_basis for item in accepted}, {"column_header"})
        self.assertFalse(accepted[0].time_inferred)

    def test_a_sentence_counting_something_else_is_not_the_measure(self) -> None:
        prose = ("There are 3,961 villages that fall within the state boundary. "
                 "Around 2,400 people live in the capital's old quarter.")
        _, (accepted, partial, _) = score("population by state in India",
                                          page_html("State population overview", [], [], prose))
        values = {item.fields.get("value"): item for item in [*accepted, *partial]}
        self.assertNotIn(values["3,961 villages"], accepted)
        self.assertIn("The sentence counts villages, not 'population'", values["3,961 villages"].reasons)
        self.assertNotIn("The sentence counts people, not 'population'", values["2,400 people"].reasons)

    def test_queries_keep_the_requested_time(self) -> None:
        plan = analyze_goal("Agricultural yeild in last 5 years", use_llm=False, today=TODAY)
        self.assertEqual(plan.search_queries[0], "Agricultural yield in last 5 years")
        self.assertTrue(all("2022-2026" in query for query in plan.search_queries[1:]))
        self.assertFalse(any(query.endswith(" in") or " in data" in query for query in plan.search_queries))

    def test_no_topic_words_in_generic_code(self) -> None:
        """String literals in the generic pipeline name no subject (site adapters excepted)."""
        banned = {"ev", "evs", "car", "cars", "vehicle", "vehicles", "crop", "crops", "wheat", "rice",
                  "hospital", "cricket", "ipl", "cardekho", "carwale"}
        offenders = []
        files = [*ROOT.glob("extraction/*.py"), *ROOT.glob("services/*.py")]
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                          if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
                          and node.body and isinstance(node.body[0], ast.Expr)
                          and isinstance(node.body[0].value, ast.Constant)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                    words = set(re.findall(r"[a-z]+", node.value.casefold()))
                    if words & banned:
                        offenders.append(f"{path.name}:{node.lineno}: {node.value[:60]!r}")
        self.assertEqual(offenders, [])
