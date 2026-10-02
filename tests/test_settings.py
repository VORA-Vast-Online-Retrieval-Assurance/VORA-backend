from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from vora.browser.settings import EngineSettings


class EngineSettingsTests(TestCase):
    def test_missing_binary_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                EngineSettings(binary_path=Path(directory) / "missing").validate()

    def test_arguments_are_deterministic_with_explicit_seed(self) -> None:
        with TemporaryDirectory() as directory:
            binary = Path(directory) / "browser"
            binary.touch()
            settings = EngineSettings(
                binary_path=binary,
                fingerprint_seed=12345,
                fingerprint_platform="windows",
                lean=False,
                http1=False,
            ).validate()
            self.assertEqual(settings.browser_arguments(), [
                "--no-sandbox",
                "--fingerprint=12345",
                "--fingerprint-platform=windows",
            ])


class LeanBrowserTests(TestCase):
    def test_lean_arguments_are_added_by_default(self) -> None:
        from vora.browser.settings import LEAN_ARGUMENTS
        settings = EngineSettings(binary_path=Path("chrome.exe"), fingerprint_seed=1, fingerprint_platform="windows")
        self.assertEqual(settings.browser_arguments()[3:], [*LEAN_ARGUMENTS, "--disable-http2"])
        self.assertTrue(settings.block_media)


class BoundedCacheTests(TestCase):
    def test_least_recently_used_entries_are_evicted(self) -> None:
        from vora.shared.cache import MISSING, BoundedCache
        cache = BoundedCache(max_entries=2)
        cache.set("a", 1)
        cache.set("b", 2)
        self.assertEqual(cache.get("a"), 1)          # "a" is now the most recently used
        cache.set("c", 3)                            # evicts "b"
        self.assertIs(cache.get("b"), MISSING)
        self.assertEqual((cache.get("a"), cache.get("c"), len(cache)), (1, 3, 2))
        self.assertEqual(cache.stats()["evictions"], 1)

    def test_entries_expire_and_none_is_a_cacheable_answer(self) -> None:
        from vora.shared.cache import MISSING, BoundedCache
        now = [0.0]
        cache = BoundedCache(max_entries=10, ttl_seconds=60, clock=lambda: now[0])
        cache.set("header", None)                     # "no concept fits" is worth remembering
        self.assertIsNone(cache.get("header"))
        now[0] = 61
        self.assertIs(cache.get("header"), MISSING)
        self.assertEqual(len(cache), 0)

    def test_the_header_mapping_cache_is_bounded(self) -> None:
        from vora.research.planning.provider import _mapping_cache
        self.assertEqual(_mapping_cache.max_entries, 20_000)
        self.assertIsNotNone(_mapping_cache.ttl_seconds)
