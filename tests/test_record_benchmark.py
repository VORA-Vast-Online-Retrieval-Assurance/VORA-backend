"""Benchmark: information that is not a number series must be accepted.

Each case is a realistic page for a different kind of request (document
registry, event catalog, company directory, software catalog, document list).
None contains a numeric measure, and none is special-cased in the code: the
same planner, parser and scorer handle all of them.
"""

import re
from datetime import date
from types import MappingProxyType
from unittest import TestCase

from vora.browser.contracts import ExecutionResult
from vora.extraction.parser import parse_rendered_page
from vora.extraction.scoring import ObservationScorer
from vora.research.planning.provider import analyze_goal

TODAY = date(2026, 9, 30)


def run(goal: str, html: str, url: str = "https://portal.example.gov/page", plan=None):
    plan = plan or analyze_goal(goal, use_llm=False, today=TODAY)
    title = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
    result = ExecutionResult(
        execution_id="bench", requested_url=url, final_url=url, title=title, html=html, status=200,
        elapsed_seconds=0.1, network_idle_reached=True,
        metadata=MappingProxyType({"fetched_at": "2026-09-30T08:00:00+00:00"}),
    )
    raw = parse_rendered_page(result)
    accepted, partial, rejected = ObservationScorer(plan, today=TODAY).score_all(raw)
    return plan, accepted, partial, rejected


def shell(title: str, body: str) -> str:
    return (f"<html><head><title>{title}</title></head><body>"
            '<header><nav class="menu"><a href="/home">Home</a><a href="/about">About Us</a>'
            '<a href="/contact">Contact</a></nav></header>'
            f"<main>{body}</main>"
            '<footer><a href="/privacy">Privacy Policy</a><a href="/terms">Terms of Use</a></footer>'
            "</body></html>")


GAZETTE = shell("eGazette - Search Gazette", """
<form id="form1" method="post" action="SearchMenu.aspx">
  <div class="menu"><a href="Default.aspx">Home</a><a href="SearchMenu.aspx">Search Gazette</a></div>
  <input type="text" name="txtDateFrom"><input type="submit" name="btnDetail" value="Search">
  <table id="tbl_Gazette">
    <tr><th>Ministry</th><th>Subject</th><th>Gazette ID</th><th>Issue Date</th><th>Download</th></tr>
    <tr><td>Ministry of Finance</td><td>Notification regarding customs duty exemption</td>
        <td>CG-DL-E-12092026-268341</td><td>12-Sep-2026</td>
        <td><a href="/WriteReadData/2026/268341.pdf">Download</a></td></tr>
    <tr><td>Ministry of Health and Family Welfare</td><td>Appointment of Joint Secretary</td>
        <td>CG-DL-E-11092026-268290</td><td>11-Sep-2026</td>
        <td><a href="/WriteReadData/2026/268290.pdf">Download</a></td></tr>
    <tr><td>Ministry of Commerce and Industry</td><td>Export policy amendment for engineering goods</td>
        <td>CG-DL-E-10092026-268211</td><td>10-Sep-2026</td>
        <td><a href="/WriteReadData/2026/268211.pdf">Download</a></td></tr>
    <tr><td>Department of Telecommunications</td><td>Rules for spectrum sharing</td>
        <td>CG-DL-E-09092026-268150</td><td>09-Sep-2026</td>
        <td><a href="/WriteReadData/2026/268150.pdf">Download</a></td></tr>
  </table>
</form>""")

EVENTS = shell("Digital Technology Expos 2024-2026", """
<h1>Upcoming technology expos</h1>
<div class="event-card"><h3>Digital Tech Expo Berlin</h3><p class="date">12-14 May 2026</p>
  <span class="city">Berlin</span><span class="country">Germany</span><a href="https://dte-berlin.example/">Official website</a></div>
<div class="event-card"><h3>Smart Systems Expo Singapore</h3><p class="date">3-5 March 2025</p>
  <span class="city">Singapore</span><span class="country">Singapore</span><a href="https://smart-systems.example/">Official website</a></div>
<div class="event-card"><h3>Future Tech Show Dubai</h3><p class="date">20-22 October 2024</p>
  <span class="city">Dubai</span><span class="country">UAE</span><a href="https://future-tech.example/">Official website</a></div>
<div class="event-card"><h3>Legacy Systems Expo Paris</h3><p class="date">4-6 June 2019</p>
  <span class="city">Paris</span><span class="country">France</span><a href="https://legacy-expo.example/">Official website</a></div>
""")

DIRECTORY = shell("AI startups in Bangalore - Directory", """
<h1>AI startups in Bangalore</h1>
<table>
  <tr><th>Company</th><th>Industry</th><th>Headquarters</th><th>Website</th></tr>
  <tr><td>Neuronest Labs</td><td>Healthcare AI</td><td>Bangalore</td><td><a href="https://neuronest.example">neuronest.example</a></td></tr>
  <tr><td>Vidya Systems</td><td>Education technology</td><td>Bangalore</td><td><a href="https://vidya.example">vidya.example</a></td></tr>
  <tr><td>Kaveri Robotics</td><td>Industrial automation</td><td>Bangalore</td><td><a href="https://kaveri.example">kaveri.example</a></td></tr>
  <tr><td>Lotus Analytics</td><td>Retail analytics</td><td>Bangalore</td><td><a href="https://lotus.example">lotus.example</a></td></tr>
</table>""")

CATALOG = shell("AI developer tools", """
<h1>AI developer tools</h1>
<table>
  <tr><th>Product</th><th>Vendor</th><th>Main features</th><th>License</th></tr>
  <tr><td>CodePilot</td><td>Acme AI</td><td>Code completion, chat, refactoring</td><td>Commercial</td></tr>
  <tr><td>OpenAssist</td><td>Community</td><td>Local models, plugins, offline mode</td><td>MIT</td></tr>
  <tr><td>DevAgent</td><td>Northwind</td><td>Autonomous tasks, pull request reviews</td><td>Apache-2.0</td></tr>
</table>""")

CIRCULARS = shell("Recent circulars - Reserve Bank", """
<h1>Recent circulars</h1>
<ul class="doc-list">
  <li><a href="/docs/RBI-2026-27-41.pdf">Master Direction on digital lending practices</a> <span>Sep 12, 2026</span></li>
  <li><a href="/docs/RBI-2026-27-40.pdf">Revised guidelines for payment aggregators</a> <span>Sep 05, 2026</span></li>
  <li><a href="/docs/RBI-2026-27-39.pdf">Priority sector lending targets for cooperative banks</a> <span>Aug 28, 2026</span></li>
  <li><a href="/docs/RBI-2026-27-38.pdf">Reporting requirements for foreign exchange transactions</a> <span>Aug 21, 2026</span></li>
</ul>""")

NAVIGATION_ONLY = shell("Welcome", """
<div class="portal-links"><a href="/services">Our Services</a><a href="/media">Media Room</a>
<a href="/careers">Careers</a><a href="/help">Help and Support</a></div>
<p>Welcome to our portal.</p>""")


def urls(rows) -> list[str]:
    return [value for row in rows for value in row.fields.values() if "://" in value or value.startswith("/")]


class RecordBenchmarkTests(TestCase):
    def test_document_registry_rows_are_accepted_without_any_number(self) -> None:
        plan, accepted, partial, _ = run("fetch me data from egazette", GAZETTE,
                                         "https://egazette.example.gov/SearchMenu.aspx")
        self.assertEqual(len(accepted), 4, [(r.status, r.reasons[-3:]) for r in partial])
        self.assertTrue(all(any(".pdf" in value for value in row.fields.values()) for row in accepted),
                        "the document link must be kept with its record")
        self.assertNotIn("value", plan.required_fields)

    def test_event_catalog_respects_the_requested_time_range(self) -> None:
        _, accepted, partial, _ = run("find digital technology expos worldwide from 2024 to 2026", EVENTS)
        names = sorted(next(v for v in row.fields.values() if "Expo" in v or "Show" in v) for row in accepted)
        self.assertEqual(names, ["Digital Tech Expo Berlin", "Future Tech Show Dubai", "Smart Systems Expo Singapore"])
        legacy = [row for row in [*accepted, *partial] if "Legacy" in " ".join(row.fields.values())]
        self.assertTrue(all(row.status != "accepted" for row in legacy))

    def test_company_directory(self) -> None:
        _, accepted, _, _ = run("AI startups in Bangalore", DIRECTORY)
        self.assertEqual(len(accepted), 4)
        self.assertTrue(all(any("://" in value for value in row.fields.values()) for row in accepted))

    def test_software_catalog(self) -> None:
        _, accepted, _, _ = run("find AI developer tools and their main features", CATALOG)
        self.assertEqual(len(accepted), 3)

    def test_document_list_of_links_and_dates(self) -> None:
        _, accepted, _, _ = run("recent RBI circulars", CIRCULARS, "https://rbi.example.org/circulars")
        self.assertEqual(len(accepted), 4)
        self.assertTrue(all(any(".pdf" in value for value in row.fields.values()) for row in accepted))

    def test_navigation_and_chrome_are_never_records(self) -> None:
        _, accepted, _, _ = run("fetch me data from egazette", NAVIGATION_ONLY)
        self.assertEqual(accepted, [])


class NamedSourceTests(TestCase):
    """A request that names a portal must reach that portal, whatever it is called."""

    def results(self):
        from vora.research.discovery.discovery import SearchResult
        return [
            SearchResult(url="https://someblog.com/how-to-fetch-data-from-egazette",
                         title="How to fetch data from egazette", snippet="dataset statistics table", rank=0),
            SearchResult(url="https://data.gov.in/catalog/gazette", title="Gazette dataset", rank=1),
            SearchResult(url="https://egazette.gov.in/", title="eGazette", snippet="Gazette of India", rank=2),
        ]

    def test_named_source_outranks_a_blog_about_it(self) -> None:
        from vora.research.discovery.ranking import rank
        plan = analyze_goal("fetch me data from egazette", use_llm=False, today=TODAY)
        self.assertEqual(plan.source_mentions, ["egazette"])
        ranked = rank(self.results(), plan, [], {})
        self.assertEqual(ranked[0].domain, "egazette.gov.in")
        self.assertIn("Site name matches 'egazette'", ranked[0].reasons)

    def test_a_site_merely_containing_a_word_is_not_the_named_source(self) -> None:
        from vora.research.discovery.ranking import site_name_match
        self.assertEqual(site_name_match("egazette.gov.in", ["egazette"]), "egazette")
        self.assertEqual(site_name_match("egazete.gov.in", ["egazette"]), "egazette")   # typo tolerated
        self.assertEqual(site_name_match("my-egazette.org", ["egazette"]), "egazette")  # hyphen piece
        self.assertIsNone(site_name_match("indiadatamap.com", ["india"]))
        self.assertIsNone(site_name_match("example.com", ["rbi"]))

    def test_search_results_are_kept_when_the_name_is_split_by_capitals(self) -> None:
        from vora.research.discovery.discovery import on_topic
        kept = on_topic(self.results(), "fetch me data from egazette")
        self.assertIn("egazette.gov.in", [item.url.split("/")[2] for item in kept])

    def test_queries_for_records_look_up_the_source_not_data_tables(self) -> None:
        plan = analyze_goal("fetch me data from egazette", use_llm=False, today=TODAY)
        self.assertIn("egazette official website", plan.search_queries)
        self.assertFalse(any("data table" in query or "statistics" in query for query in plan.search_queries))
        numeric = analyze_goal("EV car prices in India", use_llm=False, today=TODAY)
        self.assertTrue(any("data table" in query for query in numeric.search_queries))
