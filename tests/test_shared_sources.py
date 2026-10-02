"""Work shared between users: one read of a public source serves every track, rows are stored one per line, and
answers for the same request are reused. Private data (tracks, goals, conversations) is never shared."""

import os

os.environ["VORA_PROBE_SOURCES"] = "false"
os.environ["VORA_RESOLVER_SITES"] = "0"

import dataclasses  # noqa: E402
import sqlite3  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402
from tempfile import TemporaryDirectory  # noqa: E402
from unittest import TestCase  # noqa: E402
from unittest.mock import patch  # noqa: E402

import tests.test_coordinator as base  # noqa: E402  (a module import: its test classes are not collected twice)
from vora.research import coordinator as coordinator_module  # noqa: E402
from vora.research.coordinator import request_key  # noqa: E402
from vora.research.discovery.discovery import SearchResult  # noqa: E402
from vora.research.reading.source_cache import SourceCache, page_key  # noqa: E402
from vora.shared.contracts import Observation  # noqa: E402
from vora.storage.repository import Repository  # noqa: E402

PAGE = "https://prices.test/ev"


class CountingEngine(base.FakeEngine):
    reads: dict[str, int] = {}
    slow: float = 0.0

    def execute(self, url):
        CountingEngine.reads[url] = CountingEngine.reads.get(url, 0) + 1
        if CountingEngine.slow:
            time.sleep(CountingEngine.slow)
        return super().execute(url)


class SharedReadTests(TestCase):
    def setUp(self) -> None:
        base.CoordinatorTests.setUp(self)                   # the same fakes as the coordinator tests
        self.use(source_cache=True, source_ttl_page_minutes=180, source_ttl_listing_minutes=30,
                 query_cache_minutes=720)
        patcher = patch.object(coordinator_module, "BrowserEngine", CountingEngine)
        patcher.start()
        self.addCleanup(patcher.stop)
        CountingEngine.reads, CountingEngine.slow = {}, 0.0
        base.FakeEngine.pages = {PAGE: base.table_page("EV prices", [("2024", "$45,000"), ("2025", "$43,000")])}
        self.results = [SearchResult(url=PAGE, title="EV prices")]

    use = base.CoordinatorTests.use
    run_goal = base.CoordinatorTests.run_goal

    def track(self, name: str, goal: str = "EV car prices over the last 10 years") -> str:
        return self.repository.create_instance(name, goal)["id"]

    def test_a_second_user_reuses_the_read_and_scores_it_with_their_own_plan(self) -> None:
        mine, friends = self.track("me"), self.track("friend", "average electric car price by year")
        self.run_goal(mine)
        self.run_goal(friends)
        self.assertEqual(CountingEngine.reads[PAGE], 1)                       # the site was opened once
        first, second = self.repository.get_snapshot(mine), self.repository.get_snapshot(friends)
        self.assertEqual(len(second.accepted), len(first.accepted))
        outcome = next(s for s in second.sources if s.url == PAGE)
        self.assertIn("Reused a shared read", outcome.reason)
        # The friend's track holds only rows and its own plan: nothing of mine.
        self.assertNotEqual(second.plan.normalized_goal, first.plan.normalized_goal)
        self.assertEqual(self.repository.get_instance(friends)["goal"], "average electric car price by year")

    def test_a_forced_run_reads_again_and_an_old_read_is_not_reused(self) -> None:
        mine, friends = self.track("me"), self.track("friend")
        self.run_goal(mine)
        run = self.coordinator.submit(friends, fresh=True)
        self.coordinator.cancel(run["id"])                                    # not run by the pool; run it here
        self.coordinator._fresh_runs.add(run["id"])
        self.coordinator._execute(friends, run["id"], threading.Event())
        self.assertEqual(CountingEngine.reads[PAGE], 2)
        # An expired read is not reused either.
        with self.repository.connect() as db:
            db.execute("UPDATE source_reads SET fetched_at=?", ((datetime.now(UTC) - timedelta(hours=5)).isoformat(),))
        self.run_goal(self.track("later"))
        self.assertEqual(CountingEngine.reads[PAGE], 3)

    def test_two_requests_at_once_share_one_read(self) -> None:
        CountingEngine.slow = 0.6
        ids = [self.track("me"), self.track("friend")]
        threads = [threading.Thread(target=self.run_goal, args=(i,)) for i in ids]
        for thread in threads:
            thread.start()
            time.sleep(0.1)
        for thread in threads:
            thread.join(30)
        self.assertEqual(CountingEngine.reads[PAGE], 1)
        self.assertTrue(all(self.repository.get_snapshot(i).accepted for i in ids))

    def test_a_live_tracks_repeat_batch_only_takes_reads_newer_than_its_cadence(self) -> None:
        self.use(live_interval_seconds=60)
        mine = self.track("me")
        self.run_goal(mine)
        self.repository.update_instance(mine, live_enabled=True)
        with self.repository.connect() as db:      # the shared read is 10 minutes old: older than the cadence
            db.execute("UPDATE source_reads SET fetched_at=?", ((datetime.now(UTC) - timedelta(minutes=10)).isoformat(),))
        self.run_goal(mine)
        self.assertEqual(CountingEngine.reads[PAGE], 2)

    def test_the_same_request_shares_its_search_results(self) -> None:
        self.use(search_cache_minutes=60)
        searched = []
        original = coordinator_module.discover
        patcher = patch.object(coordinator_module, "discover",
                               lambda *a, **k: searched.append(1) or original(*a, **k))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.run_goal(self.track("me", "EV car prices, last 10 years"))
        self.run_goal(self.track("friend", "ev car prices last 10 years!"))
        self.assertEqual(len(searched), 1)
        self.assertEqual(request_key("EV car prices, last 10 years"), request_key("ev   car prices last 10 YEARS"))
        self.assertNotEqual(request_key("flights India to US"), request_key("flights US to India"))


class StorageTests(TestCase):
    def setUp(self) -> None:
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "t.db"
        self.repo = Repository(self.path)
        self.instance = self.repo.create_instance("t", "goal")["id"]

    def rows(self, n: int) -> list[Observation]:
        return [Observation(source_url=f"https://s.test/{i}", method="html_table", fields={"name": f"row {i}"})
                for i in range(n)]

    def test_rows_are_stored_one_per_line_and_only_changes_are_written(self) -> None:
        from vora.shared.contracts import ResearchSnapshot
        from vora.research.planning.provider import heuristic_plan

        snapshot = ResearchSnapshot(plan=heuristic_plan("goal"), accepted=self.rows(50), raw=self.rows(50))
        self.repo.save_snapshot(self.instance, snapshot)
        with self.repo.connect() as db:            # count every row write from here on
            db.executescript("""CREATE TABLE writes (n INTEGER);
                CREATE TRIGGER w1 AFTER INSERT ON snapshot_rows BEGIN INSERT INTO writes VALUES (1); END;
                CREATE TRIGGER w2 AFTER UPDATE ON snapshot_rows BEGIN INSERT INTO writes VALUES (1); END;""")
        snapshot.accepted.append(self.rows(51)[-1])
        snapshot.raw.append(self.rows(51)[-1])
        self.repo.save_snapshot(self.instance, snapshot)
        with self.repo.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM writes").fetchone()[0], 2)   # 2 new rows, not 102
        loaded = self.repo.get_snapshot(self.instance)
        self.assertEqual([r.id for r in loaded.accepted], [r.id for r in snapshot.accepted])
        snapshot.accepted = snapshot.accepted[:10]
        self.repo.save_snapshot(self.instance, snapshot)
        self.assertEqual(len(self.repo.get_snapshot(self.instance).accepted), 10)

    def test_a_snapshot_saved_before_the_rows_table_still_loads(self) -> None:
        from vora.shared.contracts import ResearchSnapshot
        from vora.research.planning.provider import heuristic_plan

        old = ResearchSnapshot(plan=heuristic_plan("goal"), accepted=self.rows(3))
        with self.repo.connect() as db:
            db.execute("INSERT INTO snapshots (instance_id, data, updated_at, accepted_count) VALUES (?,?,?,?)",
                       (self.instance, old.model_dump_json(), datetime.now(UTC).isoformat(), 3))
        self.assertEqual(len(self.repo.get_snapshot(self.instance).accepted), 3)

    def test_a_partial_listing_read_only_adds_to_a_complete_one(self) -> None:
        cache = SourceCache(self.repo)
        key = page_key("https://s.test/list")
        cache.store(key, url="https://s.test/list", final_url="https://s.test/list", title="t", status=200,
                    challenge=False, rows=self.rows(3))
        extra = self.rows(5)[3:]
        cache.store(key, url="https://s.test/list", final_url="https://s.test/list", title="t", status=200,
                    challenge=False, rows=[*self.rows(2), *extra], merge=True)
        self.assertEqual(len(cache.lookup(key, 600).rows), 5)
        self.assertIsNone(cache.lookup(key, 0))

    def test_shared_tables_hold_no_owner_goal_or_conversation(self) -> None:
        with self.repo.connect() as db:
            for table in ("source_reads", "source_rows", "query_cache"):
                columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                self.assertFalse(columns & {"owner_id", "instance_id", "goal", "content"}, table)


class CachePolicyTests(TestCase):
    def test_private_data_is_never_cached_at_the_edge(self) -> None:
        from fastapi.testclient import TestClient

        from vora.api import application

        client = TestClient(application.app)
        self.assertEqual(client.get("/version").headers["cache-control"], "public, max-age=300")
        self.assertEqual(client.get("/health").headers["cache-control"], "private, no-store")


class ListingReadTests(TestCase):
    def test_a_listing_read_that_stopped_early_never_stands_in_for_a_full_one(self) -> None:
        from types import SimpleNamespace

        from vora.learning.recipes import RecipeRun
        from vora.research.coordinator import ResearchCoordinator

        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        repo = Repository(Path(folder.name) / "t.db")
        fake = SimpleNamespace(sources=SourceCache(repo))
        rows = [Observation(source_url="https://l.test/", method="recipe", fields={"id": str(i)}) for i in range(4)]
        keep = ResearchCoordinator._keep_recipe
        keep(fake, "recipe:x", "https://l.test/", RecipeRun(ok=True, rows=rows[:1], stopped_at_known=True))
        self.assertIsNone(repo.get_source_read("recipe:x"))                  # partial and nothing to add to
        keep(fake, "recipe:x", "https://l.test/", RecipeRun(ok=True, rows=rows[:3]))
        keep(fake, "recipe:x", "https://l.test/", RecipeRun(ok=True, rows=rows[2:], stopped_at_known=True))
        self.assertEqual(len(repo.get_source_read("recipe:x")["rows"]), 4)   # the new record joined the full read
