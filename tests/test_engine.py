from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from vora.browser.engine import BrowserEngine
from vora.browser.events import EventBus, LifecycleEvent
from vora.browser.settings import EngineSettings


class BrowserEngineTests(TestCase):
    def test_execute_emits_lifecycle_and_returns_rendered_content(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        binary = Path(temporary.name) / "browser"
        binary.touch()
        events = EventBus()
        names = []
        for name in LifecycleEvent:
            events.subscribe(name, lambda event: names.append(event.name))

        request = SimpleNamespace(url="https://example.test/", method="GET", resource_type="document")
        response = SimpleNamespace(
            url=request.url,
            request=request,
            status=200,
            headers={"content-type": "text/html"},
        )
        page = Mock()
        page.url = request.url
        page.goto.return_value = response
        page.title.return_value = "Example"
        page.content.return_value = "<html>data</html>"
        handlers = {}
        page.on.side_effect = lambda event, handler: handlers.__setitem__(event, handler)
        context = Mock()
        context.new_page.return_value = page
        browser = Mock()
        browser.new_context.return_value = context

        engine = BrowserEngine(EngineSettings(binary_path=binary), events)
        engine._browser = browser
        page.goto.side_effect = lambda *args, **kwargs: (
            handlers["request"](request), handlers["response"](response), response
        )[-1]
        result = engine.execute(request.url)

        self.assertEqual(result.status, 200)
        self.assertEqual(result.title, "Example")
        self.assertEqual(result.html, "<html>data</html>")
        self.assertTrue(result.network_idle_reached)
        self.assertIs(names[0], LifecycleEvent.EXECUTION_START)
        self.assertIn(LifecycleEvent.NETWORK_REQUEST, names)
        self.assertIn(LifecycleEvent.NETWORK_RESPONSE, names)
        self.assertIs(names[-1], LifecycleEvent.EXECUTION_COMPLETE)
        context.close.assert_called_once()

    def test_context_creation_failure_emits_failed_event(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        binary = Path(temporary.name) / "browser"
        binary.touch()
        events = EventBus()
        names = []
        events.subscribe(LifecycleEvent.EXECUTION_START, lambda event: names.append(event.name))
        events.subscribe(LifecycleEvent.EXECUTION_FAILED, lambda event: names.append(event.name))
        browser = Mock()
        browser.new_context.side_effect = RuntimeError("context unavailable")
        engine = BrowserEngine(EngineSettings(binary_path=binary), events)
        engine._browser = browser

        with self.assertRaisesRegex(RuntimeError, "context unavailable"):
            engine.execute("https://example.test/")

        self.assertEqual(names, [
            LifecycleEvent.EXECUTION_START,
            LifecycleEvent.EXECUTION_FAILED,
        ])
