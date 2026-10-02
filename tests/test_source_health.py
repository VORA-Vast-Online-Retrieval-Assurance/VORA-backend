"""Sites are probed once before a batch uses them; unreachable ones are blacklisted for every user and written to data."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from vora.research.discovery import source_health as health
from vora.learning import source_registry
from vora.storage.repository import Repository


def item(url):
    return SimpleNamespace(url=url)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.repo = Repository(Path(self.dir.name) / "t.db")
        patcher = patch.dict(os.environ, {"VORA_PROBE_SOURCES": "true"})
        patcher.start()
        self.addCleanup(patcher.stop)
        health._alive.clear()


class VetTests(Base):
    def test_failing_hosts_are_dropped_and_blacklisted_for_everyone(self) -> None:
        probed = []

        def fake(host):
            probed.append(host)
            return "ConnectError" if host == "dead.example" else None

        notes = []
        results = [item("https://good.example/a"), item("https://dead.example/"), item("https://www.good.example/b")]
        kept = health.vet(self.repo, results, notes, probe_fn=fake)
        self.assertEqual([r.url for r in kept], ["https://good.example/a", "https://www.good.example/b"])
        self.assertEqual(sorted(probed), ["dead.example", "good.example"])        # one request per host
        self.assertEqual(self.repo.blacklisted_hosts(), {"dead.example"})
        self.assertIn("dead.example", notes[0])

    def test_a_blacklisted_host_is_never_requested_again_by_any_track(self) -> None:
        self.repo.blacklist("dead.example", "ConnectError")
        asked = []
        kept = health.vet(self.repo, [item("https://dead.example/x"), item("https://ok.example/")], [],
                          probe_fn=lambda host: asked.append(host))
        self.assertEqual(asked, ["ok.example"])
        self.assertEqual([r.url for r in kept], ["https://ok.example/"])

    def test_a_host_that_answered_recently_is_not_probed_again(self) -> None:
        asked = []
        for _ in range(2):
            health.vet(self.repo, [item("https://ok.example/")], [], probe_fn=lambda host: asked.append(host))
        self.assertEqual(asked, ["ok.example"])


class ProbeTests(unittest.TestCase):
    def reply(self, *outcomes):
        steps = iter(outcomes)

        def fake(url, **kwargs):
            outcome = next(steps)
            if isinstance(outcome, Exception):
                raise outcome
            return url, outcome, ""
        return patch.object(health, "safe_get", fake)

    def test_what_counts_as_an_error(self) -> None:
        with self.reply(httpx.ConnectError("x"), httpx.ConnectTimeout("x")):
            self.assertEqual(health.probe("a.example"), "ConnectTimeout")
        with self.reply(404):
            self.assertEqual(health.probe("a.example"), "HTTP 404")
        with self.reply(ValueError("private")):
            self.assertEqual(health.probe("a.example"), "not a public address")

    def test_what_does_not(self) -> None:
        for status in (200, 301, 403, 429, 503):
            with self.reply(status):
                self.assertIsNone(health.probe("a.example"), status)
        with self.reply(httpx.ReadTimeout("slow")):
            self.assertIsNone(health.probe("a.example"))                        # slow is alive
        with self.reply(httpx.ConnectError("https refused"), 200):
            self.assertIsNone(health.probe("a.example"))                        # http works


class FilesTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.registry = Path(self.dir.name) / "sources.json"
        fake = SimpleNamespace(source_registry_path=str(self.registry))
        patcher = patch.object(health, "settings", fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_export_writes_the_blacklist_and_keeps_hand_written_sources(self) -> None:
        self.registry.write_text(json.dumps({"sources": [
            {"id": "mine", "name": "Mine", "aliases": ["mine"], "domains": ["mine.example"], "entry": "https://mine.example/"},
            {"id": "auto-old.example", "name": "old.example", "auto": True, "domains": ["old.example"],
             "entry": "https://old.example/"}]}), encoding="utf-8")
        self.repo.blacklist("dead.example", "ConnectError")
        self.repo.save_learned("site.example", "https://site.example/", {"kind": "link_list", "container": "#a"})
        self.repo.save_learned("dead.example", "https://dead.example/", {"kind": "link_list", "container": "#a"})
        self.repo.save_learned("nothing.example", "https://nothing.example/", None)
        health.export_files(self.repo)
        sources = json.loads(self.registry.read_text(encoding="utf-8"))["sources"]
        self.assertEqual([s["id"] for s in sources], ["mine", "auto-site.example"])    # stale auto entry replaced
        listed = json.loads((Path(self.dir.name) / "blacklistedSources.json").read_text(encoding="utf-8"))
        self.assertEqual([h["host"] for h in listed["blacklisted"]], ["dead.example"])
        self.assertEqual({e.id for e in source_registry.load(str(self.registry))}, {"mine", "auto-site.example"})

    def test_hosts_added_to_the_file_by_hand_are_imported(self) -> None:
        (Path(self.dir.name) / "blacklistedSources.json").write_text(json.dumps({"blacklisted": [
            {"host": "Hand.Example", "reason": "slow"}, {"host": "bad/host"}, {"nope": 1}]}), encoding="utf-8")
        self.assertEqual(health.import_blacklist(self.repo), 1)
        self.assertEqual(self.repo.blacklisted_hosts(), {"hand.example"})

    def test_edits_to_the_files_are_live_without_a_restart(self) -> None:
        listed = Path(self.dir.name) / "blacklistedSources.json"
        health.export_files(self.repo)
        self.assertEqual(health.import_blacklist(self.repo, only_if_changed=True), 0)       # our own write: skipped
        listed.write_text(json.dumps({"blacklisted": [{"host": "late.example", "reason": "by hand"}]}), encoding="utf-8")
        os.utime(listed, ns=(1, health._stamp(listed) + 10**9))
        self.assertEqual(health.import_blacklist(self.repo, only_if_changed=True), 1)
        self.assertIn("late.example", self.repo.blacklisted_hosts())
        self.assertEqual(health.import_blacklist(self.repo, only_if_changed=True), 0)       # unchanged since
        self.assertEqual(source_registry.load(str(self.registry)), ())
        self.registry.write_text(json.dumps({"sources": [{"id": "n", "name": "New", "domains": ["new.example"],
                                                           "entry": "https://new.example/"}]}), encoding="utf-8")
        os.utime(self.registry, ns=(1, health._stamp(self.registry) + 10**9))
        self.assertEqual([e.id for e in source_registry.load(str(self.registry))], ["n"])

    def test_the_files_hold_no_user_data(self) -> None:
        self.repo.blacklist("dead.example", "ConnectError")
        health.export_files(self.repo)
        text = (Path(self.dir.name) / "blacklistedSources.json").read_text(encoding="utf-8")
        self.assertEqual(set(json.loads(text)["blacklisted"][0]), {"host", "reason", "listed_at"})


if __name__ == "__main__":
    unittest.main()
