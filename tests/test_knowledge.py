import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from vora.shared.contracts import GoalPlan
from vora.research.discovery.discovery import SearchResult
from vora.research.discovery import knowledge
from vora.research.discovery.knowledge import export, shared_knowledge, site_knowledge
from vora.research.discovery.ranking import rank
from vora.storage.repository import Repository


class SiteKnowledgeTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.repository = Repository(self.folder / "local.db")
        track = self.repository.create_instance("EV", "EV prices")["id"]
        self.repository.record_source(track, None, "https://stats.test/a", "stats.test", "complete", 12, 10)
        self.repository.record_source(track, None, "https://stats.test/b", "stats.test", "partial", 4, 0)
        self.repository.record_source(track, None, "https://walled.test/a", "walled.test", "blocked", 1, 0)

    def test_export_holds_only_per_site_counts_and_never_double_counts(self) -> None:
        path = self.folder / "site_knowledge.json"
        export(self.repository, path)
        export(self.repository, path)  # again: replaces this installation's section
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["contributions"]), 1)
        section = payload["contributions"][self.repository.installation_id()]
        stats = next(site for site in section["sites"] if site["domain"] == "stats.test")
        self.assertEqual(stats, {"domain": "stats.test", "reads": 2, "useful_reads": 1,
                                 "accepted_rows": 10, "blocked_reads": 0})
        self.assertEqual(set(stats), {"domain", "reads", "useful_reads", "accepted_rows", "blocked_reads"})

    def test_other_installations_add_to_local_knowledge(self) -> None:
        path = self.folder / "site_knowledge.json"
        export(self.repository, path)
        colleague = Repository(self.folder / "colleague.db")
        track = colleague.create_instance("EV", "EV prices")["id"]
        colleague.record_source(track, None, "https://stats.test/c", "stats.test", "complete", 5, 5)
        export(colleague, path)
        # Each installation sees the other's section, never its own twice.
        self.assertEqual(shared_knowledge(exclude=self.repository.installation_id(), path=path)["stats.test"]["accepted_rows"], 5)
        with patch.object(knowledge, "knowledge_path", lambda: path):
            merged = site_knowledge(self.repository)
        self.assertEqual(merged["stats.test"]["accepted_rows"], 15)
        self.assertEqual(merged["walled.test"]["blocked_reads"], 1)

    def test_ranking_uses_knowledge_from_earlier_research(self) -> None:
        plan = GoalPlan(normalized_goal="EV prices", subject_terms=["ev"])
        results = [SearchResult(url="https://walled.test/x", title="EV prices", rank=0),
                   SearchResult(url="https://new.test/x", title="EV prices", rank=1),
                   SearchResult(url="https://stats.test/x", title="EV prices", rank=2)]
        signals = {"visited": {}, "proven": {}, "blocked": {}, "known": site_knowledge(self.repository)}
        ranked = rank(results, plan, [], signals)
        self.assertEqual([item.domain for item in ranked], ["stats.test", "new.test", "walled.test"])
        self.assertTrue(any(reason.startswith("Gave data in earlier research") for reason in ranked[0].reasons))
        self.assertTrue(any(reason.startswith("Often blocks automated reading") for reason in ranked[-1].reasons))
