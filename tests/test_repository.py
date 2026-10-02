from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from vora.shared.contracts import GoalPlan, Observation, ResearchSnapshot
from vora.storage.repository import Repository


class RepositoryTests(TestCase):
    def test_instance_run_and_snapshot_round_trip(self) -> None:
        with TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "test.db")
            instance = repository.create_instance("Test", "prices")
            run = repository.create_run(instance["id"])
            snapshot = ResearchSnapshot(
                plan=GoalPlan(normalized_goal="prices"),
                raw=[Observation(source_url="https://example.test", method="html_table",
                                 fields={"price": "10"})],
            )
            repository.save_snapshot(instance["id"], snapshot)
            self.assertEqual(repository.get_run(run["id"])["status"], "queued")
            self.assertEqual(repository.get_snapshot(instance["id"]).raw[0].fields["price"], "10")



class ScaleFoundationTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "scale.db"

    def test_row_counts_do_not_need_the_snapshot_json(self) -> None:
        from vora.shared.contracts import GoalPlan, Observation, ResearchSnapshot
        repository = Repository(self.path)
        track = repository.create_instance("T", "goal")["id"]
        rows = [Observation(source_url="https://a.test", method="html_table", fields={"name": f"n{i}"},
                            status="accepted") for i in range(7)]
        repository.save_snapshot(track, ResearchSnapshot(plan=GoalPlan(normalized_goal="goal"), accepted=rows))
        self.assertEqual(repository.get_instance(track)["dataset_row_count"], 7)
        self.assertEqual(repository.list_instances(10, 0)[0]["dataset_row_count"], 7)
        with repository.connect() as db:  # the count is stored, not derived from the blob
            self.assertEqual(db.execute("SELECT accepted_count FROM snapshots").fetchone()[0], 7)

    def test_a_database_from_before_the_count_column_is_migrated(self) -> None:
        import sqlite3
        connection = sqlite3.connect(self.path)
        connection.executescript("""
            CREATE TABLE instances (id TEXT PRIMARY KEY, title TEXT NOT NULL, goal TEXT NOT NULL DEFAULT '',
              archived INTEGER NOT NULL DEFAULT 0, live_enabled INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE snapshots (instance_id TEXT PRIMARY KEY REFERENCES instances(id) ON DELETE CASCADE,
              data TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO instances VALUES ('t1','Old','g',0,0,'2026-01-01','2026-01-01');
            INSERT INTO snapshots VALUES ('t1','{"accepted":[{},{},{}]}','2026-01-01');""")
        connection.commit()
        connection.close()
        repository = Repository(self.path)
        self.assertEqual(repository.get_instance("t1")["dataset_row_count"], 3)

    def test_purge_removes_old_history_and_keeps_what_ranking_needs(self) -> None:
        from datetime import UTC, datetime, timedelta
        repository = Repository(self.path)
        track = repository.create_instance("T", "goal")["id"]
        old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
        recent_run = repository.create_run(track)
        repository.update_run(recent_run["id"], status="succeeded")
        with repository.connect() as db:
            db.execute("INSERT INTO runs VALUES ('old-run',?,'succeeded','complete','',0,0,NULL,0,?,NULL,NULL,?)",
                       (track, old, old))
            db.execute("INSERT INTO run_events (run_id, phase, detail, created_at) VALUES ('old-run','x','y',?)", (old,))
            db.execute("INSERT INTO source_history (instance_id, run_id, url, domain, status, extracted, accepted, "
                       "fetched_at) VALUES (?,?,?,?,?,?,?,?)", (track, None, "https://a.test/x", "a.test", "complete", 5, 5, old))
            db.execute("INSERT INTO source_history (instance_id, run_id, url, domain, status, extracted, accepted, "
                       "fetched_at) VALUES (?,?,?,?,?,?,?,?)", (track, None, "https://a.test/x", "a.test", "complete", 6, 6, old))
        removed = repository.purge(keep_runs=1)
        self.assertEqual((removed["runs"], removed["source_history"]), (1, 1))  # newest read of the page stays
        self.assertIsNone(repository.get_run("old-run"))
        self.assertIsNotNone(repository.get_run(recent_run["id"]))
        self.assertEqual(repository.list_run_events("old-run"), [])
        self.assertEqual(len(repository.source_signals(track, 7)["visited"]), 1)


    def test_concurrent_first_use_agrees_on_one_installation_id(self) -> None:
        import threading
        repository = Repository(self.path)
        found, failures = [], []

        def ask() -> None:
            try:
                found.append(repository.installation_id())
            except Exception as exc:  # noqa: BLE001
                failures.append(exc)

        threads = [threading.Thread(target=ask) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(failures, [])
        self.assertEqual(len(set(found)), 1)
