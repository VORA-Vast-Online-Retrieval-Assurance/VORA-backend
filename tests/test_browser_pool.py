import threading
import time
from unittest import TestCase

from vora.research.reading.browser_pool import EXPLORE, PAGE, SEARCH, BrowserPool, looks_broken

import os
os.environ["VORA_NETWORK_GUARD"] = "false"  # these tests serve pages from this machine


class FakeBrowser:
    """A browser stand-in that records where and how it was used."""

    opened: list["FakeBrowser"] = []

    def __init__(self) -> None:
        self.events = "default"
        self.thread = None
        self.tasks = 0
        self.closed = False
        FakeBrowser.opened.append(self)

    def __enter__(self):
        self.thread = threading.current_thread().name
        return self

    def __exit__(self, *args) -> None:
        self.closed = True


class BrowserPoolTests(TestCase):
    def setUp(self) -> None:
        FakeBrowser.opened = []

    def pool(self, size: int = 2, **kwargs) -> BrowserPool:
        pool = BrowserPool(FakeBrowser, size=size, **kwargs)
        self.addCleanup(pool.close)
        return pool

    def test_sequential_work_reuses_one_browser_started_on_a_worker_thread(self) -> None:
        pool = self.pool(size=3)
        used = [pool.run(lambda engine: (engine, threading.current_thread().name)) for _ in range(5)]
        self.assertEqual(len(FakeBrowser.opened), 1)                 # never more than the work needs
        self.assertEqual({thread for _, thread in used}, {FakeBrowser.opened[0].thread})
        self.assertNotEqual(FakeBrowser.opened[0].thread, threading.current_thread().name)
        self.assertEqual(pool.stats()["tasks_done"], 5)

    def test_concurrency_is_bounded_by_the_pool_size(self) -> None:
        pool = self.pool(size=2)
        lock, running, peak = threading.Lock(), [0], [0]

        def slow(engine):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.15)
            with lock:
                running[0] -= 1

        threads = [threading.Thread(target=pool.run, args=(slow,)) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(peak[0], 2)
        self.assertEqual(len(FakeBrowser.opened), 2)                 # 8 tasks, at most 2 browsers

    def test_errors_reach_the_caller_and_a_crashed_browser_is_replaced(self) -> None:
        pool = self.pool(size=1)
        pool.run(lambda engine: None)
        first = FakeBrowser.opened[0]
        with self.assertRaises(ValueError):
            pool.run(lambda engine: (_ for _ in ()).throw(ValueError("one page failed")))
        self.assertFalse(first.closed)                                # a page error is not a browser crash
        with self.assertRaises(RuntimeError):
            pool.run(lambda engine: (_ for _ in ()).throw(RuntimeError("Target closed")))
        self.assertTrue(first.closed)
        pool.run(lambda engine: None)
        self.assertEqual((len(FakeBrowser.opened), pool.stats()["restarts"]), (2, 1))

    def test_browsers_are_recycled_after_a_number_of_tasks(self) -> None:
        pool = self.pool(size=1, recycle_after=3)
        for _ in range(7):
            pool.run(lambda engine: None)
        self.assertEqual(len(FakeBrowser.opened), 3)                  # tasks 1-3, 4-6, 7
        self.assertEqual(pool.stats()["recycled"], 2)
        self.assertTrue(all(browser.closed for browser in FakeBrowser.opened[:2]))

    def test_higher_priority_work_goes_first(self) -> None:
        pool = self.pool(size=1)
        gate, order = threading.Event(), []
        holder = threading.Thread(target=pool.run, args=(lambda engine: gate.wait(5),))
        holder.start()
        time.sleep(0.1)                                               # the only worker is busy
        waiting = [threading.Thread(target=pool.run, args=(lambda engine, name=name: order.append(name),),
                                    kwargs={"priority": priority})
                   for name, priority in (("explore", EXPLORE), ("page", PAGE), ("search", SEARCH))]
        for thread in waiting:
            thread.start()
            time.sleep(0.05)
        gate.set()
        for thread in [holder, *waiting]:
            thread.join(5)
        self.assertEqual(order, ["search", "page", "explore"])

    def test_work_cancelled_while_waiting_never_runs(self) -> None:
        pool = self.pool(size=1)
        gate, ran, cancelled = threading.Event(), [], threading.Event()
        holder = threading.Thread(target=pool.run, args=(lambda engine: gate.wait(5),))
        holder.start()
        time.sleep(0.1)
        outcome = []

        def waiting() -> None:
            try:
                pool.run(lambda engine: ran.append(1), cancelled=cancelled)
            except InterruptedError as exc:
                outcome.append(str(exc))

        thread = threading.Thread(target=waiting)
        thread.start()
        time.sleep(0.1)
        cancelled.set()
        thread.join(5)
        gate.set()
        holder.join(5)
        self.assertEqual((outcome, ran), (["Run cancelled"], []))

    def test_each_batch_gets_its_own_events_on_the_shared_browser(self) -> None:
        pool = self.pool(size=1)
        seen = []
        first = pool.engine_for("events-of-batch-one")
        second = pool.engine_for("events-of-batch-two")
        first._run(lambda engine: seen.append(engine.events))
        second._run(lambda engine: seen.append(engine.events))
        pool.run(lambda engine: seen.append(engine.events))
        self.assertEqual(seen, ["events-of-batch-one", "events-of-batch-two", "default"])

    def test_closing_stops_workers_and_fails_waiting_callers(self) -> None:
        pool = BrowserPool(FakeBrowser, size=1)
        pool.run(lambda engine: None)
        gate = threading.Event()
        holder = threading.Thread(target=lambda: pool.run(lambda engine: gate.wait(5)))
        holder.start()
        time.sleep(0.1)
        errors = []
        waiter = threading.Thread(target=lambda: errors.append(_capture(pool)))
        waiter.start()
        time.sleep(0.1)
        closer = threading.Thread(target=pool.close, args=(0.3,))
        closer.start()
        closer.join(3)
        gate.set()
        waiter.join(5)
        holder.join(5)
        self.assertEqual(errors, ["The browser pool is closed"])
        with self.assertRaises(RuntimeError):
            pool.run(lambda engine: None)

    def test_broken_browser_detection(self) -> None:
        self.assertTrue(looks_broken(RuntimeError("Target page, context or browser has been closed")))
        self.assertTrue(looks_broken(Exception("Browser closed unexpectedly")))
        self.assertFalse(looks_broken(TimeoutError("Timeout 30000ms exceeded")))
        self.assertFalse(looks_broken(ValueError("bad selector")))


def _capture(pool: BrowserPool) -> str:
    try:
        pool.run(lambda engine: None)
    except RuntimeError as exc:
        return str(exc)
    return "ran"


import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import skipUnless

import vora.settings  # noqa: F401  (loads .env, where the browser path is configured)

BINARY = os.getenv("VORA_BROWSER_BINARY", "")


class _Pages(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = f"<html><head><title>page {self.path}</title></head><body><p>{self.path}</p></body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        return None


@skipUnless(BINARY and Path(BINARY).is_file(), "needs a real browser (VORA_BROWSER_BINARY)")
class RealBrowserPoolTests(TestCase):
    def test_six_pages_through_two_real_browsers(self) -> None:
        from vora.browser.engine import BrowserEngine
        from vora.browser.settings import EngineSettings
        from vora.browser.events import EventBus

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Pages)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        settings = EngineSettings.from_env()
        pool = BrowserPool(lambda: BrowserEngine(settings, EventBus()), size=2, recycle_after=4)
        self.addCleanup(pool.close)

        titles, seen_events = {}, {}

        def fetch(number: int) -> None:
            events = EventBus()
            completed = []
            from vora.browser.events import LifecycleEvent
            events.subscribe(LifecycleEvent.EXECUTION_COMPLETE, lambda event: completed.append(event.data["result"].title))
            titles[number] = pool.engine_for(events).execute(f"{base}/p{number}").title
            seen_events[number] = completed

        threads = [threading.Thread(target=fetch, args=(number,)) for number in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        self.assertEqual(titles, {number: f"page /p{number}" for number in range(6)})
        # Each page's completion went to its own batch's event bus, and only there.
        self.assertEqual(seen_events, {number: [f"page /p{number}"] for number in range(6)})
        stats = pool.stats()
        self.assertLessEqual(stats["started"], 2)
        self.assertEqual(stats["tasks_done"], 6)
