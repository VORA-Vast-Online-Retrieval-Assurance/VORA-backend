"""The browser may only reach the public internet: private addresses, redirects to them and odd schemes are refused."""

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import TestCase, skipUnless

import vora.settings  # noqa: F401  (loads .env, where the browser path is configured)
from vora.shared.urls import address_is_public, host_is_public, safe_get

BINARY = os.getenv("VORA_BROWSER_BINARY", "")


class AddressRuleTests(TestCase):
    def test_private_local_and_odd_addresses_are_not_public(self) -> None:
        for host in ("127.0.0.1", "localhost", "169.254.169.254", "10.1.2.3", "192.168.0.9", "172.16.0.1", "[::1]",
                     "0.0.0.0", "fd00::1", "", "does-not-exist.invalid"):
            with self.subTest(host=host):
                self.assertFalse(host_is_public(host))

    def test_public_addresses_are_public(self) -> None:
        self.assertTrue(address_is_public("8.8.8.8"))
        self.assertTrue(host_is_public("1.1.1.1"))

    def test_safe_get_refuses_private_targets_before_connecting(self) -> None:
        for url in ("http://127.0.0.1:8000/", "http://169.254.169.254/latest/meta-data/", "file:///etc/passwd",
                    "ftp://example.com/", "http://user:pass@example.com/", "https://example.com:8443/"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    safe_get(url)


class Page(BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        Page.hits.append(self.path)
        body = b"<html><title>local</title><body>secret local page</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        return None


@skipUnless(BINARY and Path(BINARY).is_file(), "needs a real browser (VORA_BROWSER_BINARY)")
class BrowserGuardTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/private"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def open(self, guard: bool) -> tuple[bool, list[str]]:
        import dataclasses

        from vora.browser.engine import BrowserEngine
        from vora.browser.settings import EngineSettings
        from vora.browser.engine import new_context

        Page.hits.clear()
        settings = dataclasses.replace(EngineSettings.from_env(), guard_network=guard)
        with BrowserEngine(settings) as engine:
            context = new_context(engine)
            page = context.new_page()
            try:
                page.goto(self.url, wait_until="domcontentloaded", timeout=8000)
                reached = "secret local page" in page.content()
            except Exception:  # noqa: BLE001 - a blocked navigation raises
                reached = False
            context.close()
        return reached, list(Page.hits)

    def test_a_private_address_is_never_contacted_when_the_guard_is_on(self) -> None:
        reached, hits = self.open(guard=True)
        self.assertFalse(reached)
        self.assertEqual(hits, [])          # the request never left the browser

    def test_the_same_address_works_when_the_guard_is_off(self) -> None:
        reached, hits = self.open(guard=False)
        self.assertTrue(reached)
        self.assertEqual(hits, ["/private"])
