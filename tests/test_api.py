import os

# These tests use made-up sites and a stub planner: no probing, no model calls.
os.environ["VORA_PROBE_SOURCES"] = "false"
os.environ["VORA_RESOLVER_SITES"] = "0"
os.environ["VORA_SOURCE_CACHE"] = "false"
os.environ["VORA_QUERY_CACHE_MINUTES"] = "0"
os.environ["VORA_SEARXNG_URL"] = ""   # these tests describe the built-in browser search, whatever .env says

import os
import dataclasses
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from vora.api import application
from vora.shared.contracts import GoalPlan, Observation, ResearchSnapshot, SourceOutcome
from vora.research.coordinator import ResearchCoordinator
from vora.storage.repository import Repository



class ApiTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repository = Repository(Path(self.temporary.name) / "api.db")
        self.coordinator = ResearchCoordinator(self.repository)
        for name, value in (("repository", self.repository), ("coordinator", self.coordinator),
                            ("settings", dataclasses.replace(application.settings, auth="", api_key=None))):
            patcher = patch.object(application, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(application.app)

    def _instance(self, goal: str = "EV car prices over the last 10 years") -> str:
        response = self.client.post("/api/v1/instances", json={"title": "Test", "goal": goal})
        self.assertEqual(response.status_code, 201)
        return response.json()["id"]

    def test_instance_dataset_and_frontend(self) -> None:
        instance_id = self._instance("prices")
        self.repository.save_snapshot(instance_id, ResearchSnapshot(
            plan=GoalPlan(normalized_goal="prices"),
            accepted=[Observation(source_url="https://example.test", method="html_table",
                                  fields={"name": "Item", "price": "10"}, status="accepted")],
        ))
        dataset = self.client.get(f"/api/v1/instances/{instance_id}/dataset").json()
        self.assertEqual(dataset["row_count"], 1)
        self.assertEqual(dataset["columns"], ["name", "price"])
        self.assertEqual(len(dataset["records"]), 1)
        self.assertEqual(dataset["records"][0]["source_domain"], "example.test")
        root = self.client.get("/")
        self.assertEqual(root.status_code, 200)
        self.assertEqual(root.json()["api"], "/api/v1")
        self.assertEqual(self.client.get("/src/main.js").status_code, 404)  # the web app is a separate site

    def test_rescore_upgrades_legacy_snapshot_and_exposes_provenance(self) -> None:
        instance_id = self._instance()
        legacy_plan = GoalPlan(normalized_goal="EV car price trends from 2015 to 2025",
                               required_fields=["year", "make", "model", "price_usd", "trim_level"],
                               year_start=2015, year_end=2025)
        url = "https://evdata.test/chart"
        raw = [
            Observation(source_url=url, method="html_table",
                        fields={"year": "2024", "average_pack_price": "$115/kWh", "change": "down 20%"}),
            Observation(source_url=url, method="html_table",
                        fields={"year": "2010", "average_pack_price": "$1,100/kWh"}),
            Observation(source_url=url, method="repeated_region", fields={"url": "/about", "text": "About"}),
        ]
        self.repository.save_snapshot(instance_id, ResearchSnapshot(
            plan=legacy_plan, raw=raw, partial=raw,
            sources=[SourceOutcome(id="s1", url=url, title="EV Battery Price Chart", status="partial")],
        ))
        result = self.client.post(f"/api/v1/instances/{instance_id}/dataset/rescore").json()
        self.assertEqual((result["accepted"], result["partial"], result["rejected"]), (1, 1, 1))
        self.assertEqual(result["plan"]["required_fields"], ["price", "period"])

        dataset = self.client.get(f"/api/v1/instances/{instance_id}/dataset").json()
        self.assertEqual(dataset["columns"][:3], ["period", "series", "price"])
        record = dataset["records"][0]
        self.assertEqual((record["data_period"], record["time_inferred"]), ("2024", False))
        self.assertIsNone(record["fetched_at"])  # never invented for legacy rows

        rejected = self.client.get(f"/api/v1/instances/{instance_id}/dataset/rejected").json()
        self.assertEqual(rejected["rows"][0]["content_role"], "navigation")
        stats = self.client.get(f"/api/v1/instances/{instance_id}/dataset/stats").json()
        self.assertEqual(stats["explicit_periods"], 1)
        csv = self.client.get(f"/api/v1/instances/{instance_id}/dataset/export?format=csv&provenance=true").text
        self.assertIn("time_inferred", csv.splitlines()[0])
        source = self.client.get(f"/api/v1/instances/{instance_id}/sources/s1").json()
        self.assertEqual(len(source["observations"]), 3)

    def test_run_events_are_recorded_for_each_phase_change(self) -> None:
        instance_id = self._instance()
        run = self.repository.create_run(instance_id)
        self.repository.update_run(run["id"], status="running", phase="planning", detail="Understanding the request")
        self.repository.update_run(run["id"], phase="discovery", detail="Searching: ev prices")
        self.repository.update_run(run["id"], rows_total=3)  # no phase/detail change: no event
        body = self.client.get(f"/api/v1/instances/{instance_id}/runs/{run['id']}/events").json()
        self.assertEqual([event["phase"] for event in body["events"]], ["queued", "planning", "discovery"])
        self.assertIn("scoring", body["phases"])

    def test_goal_analysis_fast_mode_is_deterministic(self) -> None:
        plan = self.client.post("/api/v1/goals/analyze?mode=fast",
                                json={"goal": "Agricultural yeild in last 5 years"}).json()
        self.assertEqual(plan["planner"], "heuristic")
        self.assertEqual(plan["required_fields"], ["yield", "period"])
        self.assertEqual(len(plan["time_scope"]["periods"]), 5)

    def test_start_run_requires_a_goal(self) -> None:
        instance_id = self._instance(goal="")
        self.assertEqual(self.client.post(f"/api/v1/instances/{instance_id}/runs").status_code, 409)

    def test_start_run_without_a_browser_explains_the_fix(self) -> None:
        instance_id = self._instance()
        not_ready = {"ready": False, "reason": "VORA_BROWSER_BINARY is not set"}
        with patch.object(self.coordinator, "readiness", lambda: not_ready):
            response = self.client.post(f"/api/v1/instances/{instance_id}/runs")
        self.assertEqual(response.status_code, 503)
        self.assertIn("VORA_BROWSER_BINARY", response.json()["detail"])
        self.assertEqual(self.repository.list_runs(instance_id), [])

    def test_live_websocket_streams_run_changes_and_rows(self) -> None:
        instance_id = self._instance()
        with self.client.websocket_connect(f"/api/v1/instances/{instance_id}/live/ws") as socket:
            hello = socket.receive_json()
            self.assertEqual(hello["type"], "hello")
            self.assertEqual(hello["state"]["batch_seconds"], application.settings.batch_seconds)
            run = self.repository.create_run(instance_id)
            self.assertEqual(socket.receive_json()["type"], "run")
            self.assertEqual(_skip_status(socket)["state"]["latest_run"]["id"], run["id"])
            self.assertEqual(socket.receive_json()["event"]["phase"], "queued")
            self.repository.update_run(run["id"], detail="Searching: EV prices")
            self.assertEqual(socket.receive_json()["run"]["detail"], "Searching: EV prices")
            event = socket.receive_json()
            self.assertEqual((event["type"], event["event"]["detail"]), ("run_event", "Searching: EV prices"))
            self.coordinator.hub.publish(instance_id, {"type": "rows", "rows": [["2025", "43000"]]})
            self.assertEqual(socket.receive_json()["rows"], [["2025", "43000"]])

    def test_live_websocket_requires_the_api_key(self) -> None:
        instance_id = self._instance()
        keyed = dataclasses.replace(application.settings, api_key="secret")
        with patch.object(application, "settings", keyed):
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect(f"/api/v1/instances/{instance_id}/live/ws") as socket:
                    socket.receive_json()
            with self.client.websocket_connect(f"/api/v1/instances/{instance_id}/live/ws?api_key=secret") as socket:
                self.assertEqual(socket.receive_json()["type"], "hello")

    def test_preferred_sources_global_and_per_track(self) -> None:
        instance_id = self._instance()
        saved = self.client.put("/api/v1/sources/preferred", json={"domains": ["https://www.FAO.org/stats", "fao.org"]})
        self.assertEqual(saved.json()["domains"], ["fao.org"])
        track = self.client.put(f"/api/v1/instances/{instance_id}/preferred-sources",
                                json={"domains": ["cardekho.com", "www.carwale.com"]}).json()
        self.assertEqual(track, {"domains": ["cardekho.com", "carwale.com"], "global": ["fao.org"]})
        bad = self.client.put(f"/api/v1/instances/{instance_id}/preferred-sources", json={"domains": ["not a site"]})
        self.assertEqual(bad.status_code, 422)
        self.assertIn("not a site", bad.json()["detail"])
        self.assertEqual(self.coordinator._preferred(instance_id), ["cardekho.com", "carwale.com", "fao.org"])

    def test_reputation_and_files_endpoints(self) -> None:
        from vora.shared.contracts import FileLink
        instance_id = self._instance()
        self.repository.record_source(instance_id, None, "https://iea.org/a", "iea.org", "blocked", 1, 0)
        self.repository.record_source(instance_id, None, "https://good.test/a", "good.test", "complete", 9, 7)
        reputation = {row["domain"]: row for row in self.client.get("/api/v1/sources/reputation").json()}
        self.assertEqual(reputation["good.test"]["accepted_rows"], 7)
        self.assertEqual(reputation["iea.org"]["blocked"], 1)

        self.repository.save_snapshot(instance_id, ResearchSnapshot(
            plan=GoalPlan(normalized_goal="prices"),
            files=[FileLink(id="f1", url="https://x.test/setup.exe", name="setup.exe", extension="exe",
                            file_type="program", reason="Programs are never downloaded", status="unsupported"),
                   FileLink(id="f2", url="https://x.test/r.pdf", name="r.pdf", extension="pdf",
                            file_type="document", extractable=True, relevance=0.9)]))
        listing = self.client.get(f"/api/v1/instances/{instance_id}/files").json()
        self.assertEqual([item["id"] for item in listing["files"]], ["f2", "f1"])
        self.assertEqual(listing["counts"], {"document": 1, "program": 1})
        refused = self.client.post(f"/api/v1/instances/{instance_id}/files/f1/extract")
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(self.client.post(f"/api/v1/instances/{instance_id}/files/nope/extract").status_code, 404)

    def test_operational_endpoints(self) -> None:
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/version").json()["name"], "VORA")
        capabilities = self.client.get("/api/v1/capabilities").json()
        self.assertTrue(capabilities["event_driven"])
        self.assertIn("text_statement", capabilities["extractors"])
        self.assertIn("authentication_required", self.client.get("/api/v1/auth/session").json())


def _skip_status(socket) -> dict:
    """The first run of a track changes the latest status, so a status frame follows."""
    message = socket.receive_json()
    assert message["type"] == "status", message
    return message


class SearchAndReadinessApiTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        repository = Repository(Path(self.temporary.name) / "api.db")
        for name, value in (("repository", repository), ("coordinator", ResearchCoordinator(repository)),
                            ("settings", dataclasses.replace(application.settings, auth="", api_key=None))):
            patcher = patch.object(application, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(application.app)

    def test_search_status_reports_resting_engines(self) -> None:
        from vora.research.discovery.discovery import search_gate
        search_gate.reset()
        self.addCleanup(search_gate.reset)
        search_gate.record("duckduckgo", blocked=True)
        status = self.client.get("/api/v1/search/status").json()
        engines = {engine["name"]: engine for engine in status["engines"]}
        self.assertGreater(engines["duckduckgo"]["resting_seconds"], 0)
        self.assertEqual(engines["bing"]["resting_seconds"], 0)
        self.assertIn("max_searches_per_hour", status)

    def test_readiness_and_capabilities_describe_batches(self) -> None:
        ready = self.client.get("/api/v1/ready").json()
        self.assertIn("free_memory_mb", ready)
        self.assertIn(ready["search"], {"browser", "brave", "google"})
        capabilities = self.client.get("/api/v1/capabilities").json()
        for key in ("batch_seconds", "live_interval_seconds", "deep_lane", "max_searches_per_hour", "source_statuses"):
            self.assertIn(key, capabilities)
        self.assertIn("network_json", capabilities["extractors"])
