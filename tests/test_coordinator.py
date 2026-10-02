import os

# These tests use made-up sites and a stub planner: no probing, no model calls.
os.environ["VORA_PROBE_SOURCES"] = "false"
os.environ["VORA_RESOLVER_SITES"] = "0"
os.environ["VORA_SOURCE_CACHE"] = "false"
os.environ["VORA_QUERY_CACHE_MINUTES"] = "0"

import os
import dataclasses
import threading
import time
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from unittest import TestCase
from unittest.mock import patch

from vora.browser.contracts import ExecutionResult
from vora.browser.events import LifecycleEvent, RuntimeEvent
from vora.browser.explore import CapturedResponse, Exploration
from vora.research.planning.provider import heuristic_plan
from vora.research import coordinator as coordinator_module
from vora.research.coordinator import ResearchCoordinator
from vora.research.reading.deep_lane import HostGate
from vora.research.discovery.discovery import SearchResult
from vora.storage.repository import Repository


CHALLENGE = "<html><head><title>Just a moment...</title></head><body>Performing security verification</body></html>"


def table_page(title: str, rows: list[tuple[str, str]]) -> str:
    body = "".join(f"<tr><td>{year}</td><td>{price}</td></tr>" for year, price in rows)
    return (f"<html><head><title>{title}</title></head><body><main><h2>EV prices</h2>"
            f"<table><tr><th>Year</th><th>Average EV price</th></tr>{body}</table>"
            f'<a href="/files/ev-report.pdf">EV price report 2025 (PDF)</a>'
            f'<a href="/setup.exe">Installer</a></main></body></html>')


class FakeEngine:
    """Stands in for BrowserEngine: returns canned HTML and emits completion events.

    ``hidden`` holds the extra states an interactive pass reveals per URL and
    ``captured`` the JSON a page loads.
    """

    pages: dict[str, str] = {}
    hidden: dict[str, list[str]] = {}
    captured: dict[str, list[CapturedResponse]] = {}
    barrier: "threading.Barrier | None" = None   # opens only when two pages render at the same moment

    created = 0

    def __init__(self, settings, events) -> None:
        self.events = events
        FakeEngine.created += 1

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def execute(self, url: str) -> ExecutionResult:
        if FakeEngine.barrier is not None and url in self.pages:
            FakeEngine.barrier.wait()
        html = self.pages.get(url, "<html><body>empty</body></html>")
        result = ExecutionResult(
            execution_id=f"exec-{abs(hash(url))}", requested_url=url, final_url=url, title=url, html=html,
            status=200, elapsed_seconds=0.01, network_idle_reached=True,
            metadata=MappingProxyType({"fetched_at": "2026-09-29T08:00:00+00:00",
                                       "response_headers": MappingProxyType({})}),
        )
        self.events.emit(RuntimeEvent(LifecycleEvent.EXECUTION_COMPLETE, result.execution_id,
                                      data=MappingProxyType({"result": result})))
        return result

    def explore(self, url: str, budget_seconds: float = 30, should_stop=None, hints=None) -> Exploration:
        states = []
        for index, html in enumerate(self.hidden.get(url, [])):
            state = ExecutionResult(
                execution_id=f"deep-{abs(hash(url))}-{index}", requested_url=url, final_url=url, title=url,
                html=html, status=200, elapsed_seconds=0.01, network_idle_reached=True,
                metadata=MappingProxyType({"fetched_at": "2026-09-29T08:00:00+00:00", "state": f"tab {index}",
                                           "response_headers": MappingProxyType({})}))
            self.events.emit(RuntimeEvent(LifecycleEvent.EXECUTION_COMPLETE, state.execution_id,
                                          data=MappingProxyType({"result": state})))
            states.append(state)
        return Exploration(states=states, captured=list(self.captured.get(url, [])),
                           actions=[f"opened tab {index}" for index in range(len(states))])


class CoordinatorTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repository = Repository(Path(self.temporary.name) / "coordinator.db")
        self.coordinator = ResearchCoordinator(self.repository)
        self.addCleanup(self.coordinator.shutdown)  # closes the shared browser pool
        settings = dataclasses.replace(coordinator_module.settings, max_sources=2, max_candidates=10,
                                       semantic_llm=False, merge_runs=True, max_linked_datasets=0,
                                       search_cache_minutes=0, deep_lane=False,
                                       source_cache=False, query_cache_minutes=0)
        self.results: list[SearchResult] = []
        for target, value in (
            ("settings", settings), ("BrowserEngine", FakeEngine),
            ("analyze_goal", lambda goal: heuristic_plan(goal, today=date(2026, 9, 29))),
            ("discover", lambda engine, plan, limit, preferred=None, **kwargs: list(self.results)),
            ("ensure_public_url", lambda url: url),
        ):
            patcher = patch.object(coordinator_module, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(ResearchCoordinator, "_engine_settings", lambda self: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(HostGate, "wait", lambda self, url: None)  # no politeness delay in tests
        patcher.start()
        self.addCleanup(patcher.stop)
        # The host's free memory must not decide whether the interactive pass runs in tests.
        patcher = patch.object(coordinator_module, "available_memory_mb", lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        FakeEngine.hidden, FakeEngine.captured, FakeEngine.barrier = {}, {}, None

    def use(self, **changes) -> None:
        patcher = patch.object(coordinator_module, "settings",
                               dataclasses.replace(coordinator_module.settings, **changes))
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_goal(self, instance_id: str) -> None:
        run = self.repository.create_run(instance_id)
        self.coordinator._execute(instance_id, run["id"], threading.Event())

    def test_blocked_page_is_backfilled_and_history_recorded(self) -> None:
        FakeEngine.pages = {
            "https://blocked.test/ev": CHALLENGE,
            "https://one.test/ev": table_page("EV prices one", [("2024", "$45,000"), ("2025", "$43,000")]),
            "https://two.test/ev": table_page("EV prices two", [("2023", "$47,000")]),
            "https://three.test/ev": table_page("EV prices three", [("2022", "$49,000")]),
        }
        self.results = [SearchResult(url=url, title="EV prices", rank=index)
                        for index, url in enumerate(FakeEngine.pages)]
        instance = self.repository.create_instance("EV", "EV car prices over the last 10 years")
        self.run_goal(instance["id"])

        snapshot = self.repository.get_snapshot(instance["id"])
        statuses = {source.domain: source.status for source in snapshot.sources}
        self.assertEqual(statuses["blocked.test"], "blocked")
        self.assertEqual((statuses["one.test"], statuses["two.test"]), ("complete", "complete"))
        self.assertEqual(statuses["three.test"], "skipped")  # budget of 2 usable pages reached
        skipped = next(source for source in snapshot.sources if source.status == "skipped")
        self.assertTrue(skipped.rank_reasons)
        self.assertEqual(len(snapshot.accepted), 3)
        signals = self.repository.source_signals(instance["id"], 7)
        self.assertIn("blocked.test", signals["blocked"])
        self.assertEqual(signals["proven"]["one.test"], 2)

        files = {item.name: item for item in snapshot.files}
        self.assertEqual(files["ev-report.pdf"].status, "found")
        self.assertTrue(files["ev-report.pdf"].extractable)
        self.assertEqual(files["setup.exe"].status, "unsupported")
        self.assertEqual(files["setup.exe"].reason, "Programs are never downloaded")

    def test_same_goal_merges_and_new_goal_starts_fresh(self) -> None:
        FakeEngine.pages = {"https://one.test/ev": table_page("EV one", [("2024", "$45,000")]),
                            "https://two.test/ev": table_page("EV two", [("2023", "$47,000")])}
        self.results = [SearchResult(url="https://one.test/ev", title="EV prices")]
        instance = self.repository.create_instance("EV", "EV car prices over the last 10 years")
        self.run_goal(instance["id"])
        self.assertEqual(len(self.repository.get_snapshot(instance["id"]).accepted), 1)

        # Second run of the same goal reads a different page: rows accumulate.
        self.results = [SearchResult(url="https://two.test/ev", title="EV prices")]
        self.run_goal(instance["id"])
        merged = self.repository.get_snapshot(instance["id"])
        self.assertEqual(sorted(item.data_period for item in merged.accepted), ["2023", "2024"])

        # Re-reading a page replaces its old rows instead of duplicating them.
        FakeEngine.pages["https://two.test/ev"] = table_page("EV two", [("2023", "$46,500")])
        self.run_goal(instance["id"])
        refreshed = self.repository.get_snapshot(instance["id"])
        self.assertEqual(len(refreshed.accepted), 2)
        self.assertIn("$46,500", [item.normalized.get("price") for item in refreshed.accepted])

        # A different goal starts a fresh dataset.
        self.repository.update_instance(instance["id"], goal="EV battery prices over the last 3 years")
        self.results = [SearchResult(url="https://one.test/ev", title="EV prices")]
        self.run_goal(instance["id"])
        fresh = self.repository.get_snapshot(instance["id"])
        self.assertEqual({item.source_url for item in fresh.raw}, {"https://one.test/ev"})

    def test_extract_file_on_request(self) -> None:
        FakeEngine.pages = {"https://one.test/ev": table_page("EV one", [("2024", "$45,000")])}
        self.results = [SearchResult(url="https://one.test/ev", title="EV prices")]
        instance = self.repository.create_instance("EV", "EV car prices over the last 10 years")
        self.run_goal(instance["id"])
        pdf = next(item for item in self.repository.get_snapshot(instance["id"]).files if item.extension == "pdf")
        report = (Path(__file__).parent / "fixtures" / "crop_report.pdf").read_bytes()

        from vora.output.datasets import DownloadedFile
        downloaded = DownloadedFile(pdf.url, report, "application/pdf", None, "2026-09-29T09:00:00+00:00")
        with patch.object(coordinator_module, "download_file", lambda url: downloaded):
            updated = self.coordinator.extract_file(instance["id"], pdf.id)
        self.assertEqual(updated.status, "extracted")
        self.assertGreater(updated.extracted_rows, 0)
        snapshot = self.repository.get_snapshot(instance["id"])
        self.assertTrue(any(item.source_url == pdf.url for item in snapshot.raw))
        exe = next(item for item in snapshot.files if item.extension == "exe")
        with self.assertRaises(PermissionError):
            self.coordinator.extract_file(instance["id"], exe.id)

    def test_shutdown_stops_an_active_run_at_its_next_checkpoint(self) -> None:
        started, release = threading.Event(), threading.Event()

        class SlowEngine(FakeEngine):
            def execute(self, url: str) -> ExecutionResult:
                started.set()
                release.wait(5)
                return super().execute(url)

        FakeEngine.pages = {f"https://site{index}.test/ev": table_page("EV prices", [("2025", "$43,000")])
                            for index in range(3)}
        self.results = [SearchResult(url=url, title="EV prices", rank=index)
                        for index, url in enumerate(FakeEngine.pages)]
        instance = self.repository.create_instance("EV", "EV car prices over the last 10 years")
        with patch.object(coordinator_module, "BrowserEngine", SlowEngine):
            run = self.coordinator.submit(instance["id"])
            self.assertTrue(started.wait(5))
            self.coordinator.shutdown()
            release.set()
            deadline = time.monotonic() + 5
            # Terminal status, and the worker is done with the database (its
            # last step drops the run from ``_cancel``).
            while (self.repository.get_run(run["id"])["status"] not in coordinator_module.TERMINAL
                   or run["id"] in self.coordinator._cancel):
                self.assertLess(time.monotonic(), deadline, "run did not stop after shutdown")
                time.sleep(0.05)

        self.assertEqual(self.repository.get_run(run["id"])["status"], "cancelled")
        self.assertEqual(self.coordinator._pool.submit(lambda: 1).result(timeout=1), 1)

    def test_interactive_pass_adds_hidden_rows_chart_data_and_a_linked_page(self) -> None:
        self.use(deep_lane=True, crawl_per_site=1)
        link = '<a href="/ev/2022-prices">EV prices 2022 table</a><a href="/careers">Careers</a>'
        FakeEngine.pages = {"https://one.test/ev": table_page("EV prices one", [("2024", "$45,000")]).replace(
            "</main>", link + "</main>")}
        FakeEngine.hidden = {
            "https://one.test/ev": [FakeEngine.pages["https://one.test/ev"],
                                    table_page("EV prices one", [("2023", "$44,000")])],
            "https://one.test/ev/2022-prices": [table_page("EV prices 2022", [("2022", "$46,000")])],
        }
        FakeEngine.captured = {"https://one.test/ev": [CapturedResponse(
            "https://one.test/api/prices", "application/json",
            b'[{"year": "2021", "average_ev_price": "$48,000"}]')]}
        self.results = [SearchResult(url="https://one.test/ev", title="EV prices")]
        instance = self.repository.create_instance("EV", "EV car prices over the last 10 years")
        self.run_goal(instance["id"])

        snapshot = self.repository.get_snapshot(instance["id"])
        self.assertEqual(sorted(item.data_period for item in snapshot.accepted), ["2021", "2022", "2023", "2024"])
        self.assertIn("network_json", {item.method for item in snapshot.accepted})
        page = next(source for source in snapshot.sources if source.url == "https://one.test/ev")
        self.assertEqual((page.status, page.deep_accepted), ("complete", 2))
        self.assertIn("opened tab 1", page.deep_notes)
        crawl = next(source for source in snapshot.sources if source.origin == "crawl")
        self.assertEqual((crawl.url, crawl.linked_from, crawl.status),
                         ("https://one.test/ev/2022-prices", "https://one.test/ev", "complete"))
        self.assertFalse(any("careers" in source.url for source in snapshot.sources))

    def test_empty_pages_do_not_use_the_page_budget(self) -> None:
        FakeEngine.pages = {
            "https://empty1.test/ev": "<html><head><title>EV</title></head><body><main>Nothing here</main></body></html>",
            "https://empty2.test/ev": "<html><head><title>EV</title></head><body><main>Nothing</main></body></html>",
            "https://one.test/ev": table_page("EV prices one", [("2024", "$45,000")]),
            "https://two.test/ev": table_page("EV prices two", [("2023", "$47,000")]),
        }
        self.results = [SearchResult(url=url, title="EV prices", rank=index)
                        for index, url in enumerate(FakeEngine.pages)]
        instance = self.repository.create_instance("EV", "EV car prices over the last 10 years")
        self.run_goal(instance["id"])
        statuses = {source.domain: source.status for source in self.repository.get_snapshot(instance["id"]).sources}
        self.assertEqual(statuses, {"empty1.test": "empty", "empty2.test": "empty",
                                    "one.test": "complete", "two.test": "complete"})

    def test_time_budget_queues_the_remaining_pages(self) -> None:
        self.use(batch_seconds=1)
        FakeEngine.pages = {"https://one.test/ev": table_page("EV prices", [("2024", "$45,000")])}
        self.results = [SearchResult(url="https://one.test/ev", title="EV prices")]
        instance = self.repository.create_instance("EV", "EV car prices over the last 10 years")
        self.run_goal(instance["id"])
        source = self.repository.get_snapshot(instance["id"]).sources[0]
        self.assertEqual(source.status, "skipped")
        self.assertIn("time budget", source.reason)
        messages = self.repository.list_messages(instance["id"], 10, 0)
        self.assertIn("Diagnostics:", messages[-1]["content"])

    def test_live_batches_start_on_a_fixed_cadence(self) -> None:
        from datetime import UTC, datetime, timedelta
        instance = self.repository.create_instance("EV", "EV prices")
        self.assertIsNotNone(self.coordinator.next_cycle_at(instance["id"], True))  # no batch yet: now
        run = self.repository.create_run(instance["id"])
        now = datetime.now(UTC)
        started, finished = now - timedelta(seconds=100), now - timedelta(seconds=10)
        self.repository.update_run(run["id"], status="succeeded", started_at=started.isoformat(),
                                   finished_at=finished.isoformat())
        interval = coordinator_module.settings.live_interval_seconds
        self.assertEqual(self.coordinator.next_cycle_at(instance["id"], True), started + timedelta(seconds=interval))
        # A batch that ran past its slot is followed at once, never overlapped.
        self.repository.update_run(run["id"], started_at=(now - timedelta(seconds=interval + 60)).isoformat())
        self.assertEqual(self.coordinator.next_cycle_at(instance["id"], True), finished)
        self.assertIsNone(self.coordinator.next_cycle_at(instance["id"], False))

    def test_missing_browser_fails_with_the_fix(self) -> None:
        def missing(_self):
            raise ValueError("VORA_BROWSER_BINARY must point to a Chrome or Chromium executable")
        with patch.object(ResearchCoordinator, "_engine_settings", missing):
            instance = self.repository.create_instance("EV", "EV car prices over the last 10 years")
            run = self.repository.create_run(instance["id"])
            self.coordinator._execute(instance["id"], run["id"], threading.Event())
        stored = self.repository.get_run(run["id"])
        self.assertEqual(stored["status"], "failed")
        self.assertTrue(stored["detail"].startswith("Browser not available"))
        self.assertIn("VORA_BROWSER_BINARY", self.repository.list_messages(instance["id"], 5, 0)[-1]["content"])

    def test_search_results_are_reused_while_unread(self) -> None:
        self.use(search_cache_minutes=60, max_sources=1)
        calls = []
        FakeEngine.pages = {f"https://site{index}.test/ev": table_page("EV prices", [("2024", "$45,000")])
                            for index in range(3)}
        results = [SearchResult(url=url, title="EV prices", rank=index) for index, url in enumerate(FakeEngine.pages)]

        def discover(engine, plan, limit, preferred=None, **kwargs):
            calls.append(plan.normalized_goal)
            return list(results)

        with patch.object(coordinator_module, "discover", discover):
            instance = self.repository.create_instance("EV", "EV car prices over the last 10 years")
            self.run_goal(instance["id"])
            self.run_goal(instance["id"])
        self.assertEqual(len(calls), 1)
        read = [source.url for source in self.repository.get_snapshot(instance["id"]).sources
                if source.status == "complete"]
        self.assertEqual(len(read), 2)
        events = [event["detail"] for run in self.repository.list_runs(instance["id"])
                  for event in self.repository.list_run_events(run["id"])]
        self.assertTrue(any(detail.startswith("Reusing search results") for detail in events))

    def test_a_language_model_plan_is_reused_by_later_batches_of_the_same_goal(self) -> None:
        calls = []
        from vora.extraction.temporal import today_utc  # plans are dated in UTC, like the coordinator
        llm_plan = heuristic_plan("EV car prices over the last 10 years", today=today_utc()).model_copy(
            update={"planner": "llm:test-model"})

        def planner(goal):
            calls.append(goal)
            return llm_plan

        FakeEngine.pages = {"https://one.test/ev": table_page("EV prices", [("2024", "$45,000")])}
        self.results = [SearchResult(url="https://one.test/ev", title="EV prices")]
        with patch.object(coordinator_module, "analyze_goal", planner):
            instance = self.repository.create_instance("EV", "EV car prices over the last 10 years")
            self.run_goal(instance["id"])
            self.run_goal(instance["id"])
            self.repository.update_instance(instance["id"], goal="EV battery prices over the last 3 years")
            self.run_goal(instance["id"])
        self.assertEqual(len(calls), 2)  # second batch reused the plan; a new goal plans again

    def test_search_summary_counts_each_outcome(self) -> None:
        self.assertEqual(coordinator_module.summarize_search([
            "duckduckgo: blocked by a verification page", "bing: no results", "bing: 12 results",
            "bing: blocked (results unrelated to the query)"]),
            "duckduckgo: 1 blocked; bing: 1 empty, 1 ok, 1 blocked")

    def test_captured_json_must_be_about_the_request(self) -> None:
        from vora.research.reading.deep_lane import relevant_response, request_words
        plan = heuristic_plan("number of hospitals per state in India", today=date(2026, 9, 29))
        words = request_words(plan)
        self.assertIn("hospital", words)
        self.assertNotIn("india", words)
        chart = b'[{"state": "Kerala", "hospitals": 1280}]'
        cookies = b'{"Groups": [{"GroupName": "Performance Cookies", "Order": 3}]}'
        self.assertTrue(relevant_response("https://stats.test/api/chart/12", chart, words))
        self.assertFalse(relevant_response("https://stats.test/api/chart/13", cookies, words))
        self.assertFalse(relevant_response("https://cdn.cookielaw.org/consent/abc.json", chart, words))

    def test_without_search_old_results_then_earlier_pages_then_known_sites(self) -> None:
        self.use(search_cache_minutes=0, max_sources=1)
        FakeEngine.pages = {"https://one.test/ev": table_page("EV prices", [("2024", "$45,000")])}
        instance = self.repository.create_instance("EV", "EV car prices over the last 10 years")
        self.results = [SearchResult(url="https://one.test/ev", title="EV prices")]
        self.run_goal(instance["id"])  # search works: results are cached

        self.results = []  # search now blocked
        self.run_goal(instance["id"])
        events = [event["detail"] for run in self.repository.list_runs(instance["id"])[:1]
                  for event in self.repository.list_run_events(run["id"])]
        self.assertTrue(any(detail.startswith("Search unavailable; reusing results") for detail in events))

        self.repository.clear_snapshot(instance["id"])  # also drops the cached search
        self.repository.set_preferred(instance["id"], ["stats.test"])
        self.run_goal(instance["id"])
        sources = {source.url for source in self.repository.get_snapshot(instance["id"]).sources}
        self.assertIn("https://one.test/ev", sources)       # gave data before
        self.assertIn("https://stats.test/", sources)       # preferred site's home page

    def test_interactive_browser_starts_only_when_it_has_a_page(self) -> None:
        self.use(deep_lane=True)
        FakeEngine.pages = {"https://blocked.test/ev": CHALLENGE}
        self.results = [SearchResult(url="https://blocked.test/ev", title="EV prices")]
        instance = self.repository.create_instance("EV", "EV car prices over the last 10 years")
        FakeEngine.created = 0
        self.run_goal(instance["id"])
        self.assertEqual(FakeEngine.created, 1)  # only the fast pass's browser

    def test_interactive_pass_is_skipped_when_memory_is_short(self) -> None:
        self.use(deep_lane=True, deep_lane_min_free_mb=1500)
        FakeEngine.pages = {"https://one.test/ev": table_page("EV prices", [("2024", "$45,000")])}
        FakeEngine.hidden = {"https://one.test/ev": [table_page("EV prices", [("2023", "$44,000")])]}
        self.results = [SearchResult(url="https://one.test/ev", title="EV prices")]
        instance = self.repository.create_instance("EV", "EV car prices over the last 10 years")
        with patch.object(coordinator_module, "available_memory_mb", lambda: 900):
            self.run_goal(instance["id"])
        snapshot = self.repository.get_snapshot(instance["id"])
        self.assertEqual([item.data_period for item in snapshot.accepted], ["2024"])
        self.assertIn("interactive pass skipped: 900 MB free memory (needs 1500)",
                      self.repository.list_messages(instance["id"], 5, 0)[-1]["content"])

    def _run_two_tracks_together(self) -> list[dict]:
        self.use(deep_lane=False, max_sources=1)
        goals = {"EV car prices over the last 10 years": "https://one.test/ev",
                 "EV battery prices over the last 5 years": "https://two.test/ev"}
        FakeEngine.pages = {url: table_page("EV prices", [("2024", "$45,000")]) for url in goals.values()}
        instances = [self.repository.create_instance("T", goal) for goal in goals]
        runs = [self.repository.create_run(instance["id"]) for instance in instances]
        discover = lambda engine, plan, limit, preferred=None, **kwargs: [  # noqa: E731
            SearchResult(url=goals[plan.normalized_goal], title="EV prices")]
        with patch.object(coordinator_module, "discover", discover):
            workers = [threading.Thread(target=self.coordinator._execute,
                                        args=(instance["id"], run["id"], threading.Event()))
                       for instance, run in zip(instances, runs)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(30)
        return [self.repository.get_run(run["id"]) for run in runs]

    def test_tracks_run_at_the_same_time_on_shared_browsers(self) -> None:
        self.use(browser_pool_size=2)
        # The barrier opens only if both tracks are rendering at the same moment. With one
        # global browser lock (the old design) the first would wait for the second forever.
        FakeEngine.barrier = threading.Barrier(2, timeout=10)
        runs = self._run_two_tracks_together()
        self.assertFalse(FakeEngine.barrier.broken)
        self.assertEqual([run["status"] for run in runs], ["succeeded", "succeeded"])

    def test_many_tracks_never_need_more_browsers_than_the_pool(self) -> None:
        self.use(browser_pool_size=1)
        FakeEngine.created = 0
        runs = self._run_two_tracks_together()
        self.assertEqual([run["status"] for run in runs], ["succeeded", "succeeded"])
        self.assertEqual(FakeEngine.created, 1)                    # two tracks, one browser
        stats = self.coordinator.pool_stats()
        self.assertEqual((stats["size"], stats["started"], stats["tasks_done"] >= 2), (1, 1, True))

    def test_readiness_reports_the_browser_pool(self) -> None:
        state = self.coordinator.readiness()
        self.assertEqual(set(state["browsers"]) & {"active_batches", "size", "busy", "queued", "restarts"},
                         {"active_batches", "size", "busy", "queued", "restarts"})
