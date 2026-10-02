import os

# These tests use made-up sites and a stub planner: no probing, no model calls.
os.environ["VORA_PROBE_SOURCES"] = "false"
os.environ["VORA_RESOLVER_SITES"] = "0"
os.environ["VORA_SOURCE_CACHE"] = "false"
os.environ["VORA_QUERY_CACHE_MINUTES"] = "0"

import os
"""Sign-in: Supabase access tokens, per-user tracks, CORS.

Tokens are signed here with a key made for the test, so no real credential is involved:
the verifier is given that public key instead of fetching the project's key set.
"""

import dataclasses
import secrets
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from vora.api import application
from vora.api.auth import verifier
from vora.research.coordinator import ResearchCoordinator
from vora.storage.repository import Repository


PROJECT = "https://project.supabase.co"
ALICE, BOB = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
KEY = ec.generate_private_key(ec.SECP256R1())
OTHER_KEY = ec.generate_private_key(ec.SECP256R1())


def token(user: str = ALICE, *, key=KEY, expires_in: int = 3600, audience: str = "authenticated",
          issuer: str = f"{PROJECT}/auth/v1", algorithm: str = "ES256", email: str | None = None) -> str:
    claims = {"sub": user, "aud": audience, "iss": issuer, "exp": int(time.time()) + expires_in,
              "role": "authenticated"}
    if email:
        claims["email"] = email
    return jwt.encode(claims, key, algorithm=algorithm)


def as_user(user: str = ALICE, **kwargs) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(user, **kwargs)}"}


class SupabaseAuthTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repository = Repository(Path(self.temporary.name) / "auth.db")
        self.coordinator = ResearchCoordinator(self.repository)
        self.secret = secrets.token_hex(16)
        configured = dataclasses.replace(application.settings, auth="supabase", supabase_url=PROJECT,
                                         api_key=None, supabase_jwt_secret=self.secret)
        for target, name, value in (
                (application, "repository", self.repository), (application, "coordinator", self.coordinator),
                (application, "settings", configured)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Trust the test key (the real verifier would fetch the project's public keys).
        patcher = patch.object(verifier, "signing_key", lambda raw, url: KEY.public_key())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(application.app)

    def make(self, user: str = ALICE, title: str = "Track", goal: str = "prices") -> str:
        response = self.client.post("/api/v1/instances", json={"title": title, "goal": goal}, headers=as_user(user))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    # -- who may call ---------------------------------------------------------

    def test_a_token_is_required(self) -> None:
        self.assertEqual(self.client.get("/api/v1/instances").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/instances", headers={"Authorization": "Basic abc"}).status_code, 401)
        self.assertEqual(self.client.get("/api/v1/instances", headers={"Authorization": "Bearer not-a-token"}).status_code, 401)
        self.assertEqual(self.client.get("/api/v1/instances", headers=as_user()).status_code, 200)

    def test_bad_tokens_are_rejected(self) -> None:
        for headers in (as_user(expires_in=-3600), as_user(audience="anon"),
                        as_user(issuer="https://evil.example/auth/v1"), as_user(key=OTHER_KEY)):
            with self.subTest(headers=headers):
                # the wrong key: the verifier is given the trusted key, so a token signed by another key fails
                with patch.object(verifier, "signing_key", lambda raw, url: KEY.public_key()):
                    self.assertEqual(self.client.get("/api/v1/instances", headers=headers).status_code, 401)

    def test_a_shared_secret_token_needs_the_secret_and_the_right_signature(self) -> None:
        good = jwt.encode({"sub": ALICE, "aud": "authenticated", "iss": f"{PROJECT}/auth/v1",
                           "exp": int(time.time()) + 600}, self.secret, algorithm="HS256")
        bad = jwt.encode({"sub": ALICE, "aud": "authenticated", "iss": f"{PROJECT}/auth/v1",
                          "exp": int(time.time()) + 600}, secrets.token_hex(16), algorithm="HS256")
        self.assertEqual(self.client.get("/api/v1/instances", headers={"Authorization": f"Bearer {good}"}).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/instances", headers={"Authorization": f"Bearer {bad}"}).status_code, 401)
        # an unsigned token must never pass
        none = jwt.encode({"sub": ALICE, "aud": "authenticated", "iss": f"{PROJECT}/auth/v1",
                           "exp": int(time.time()) + 600}, None, algorithm="none")
        self.assertEqual(self.client.get("/api/v1/instances", headers={"Authorization": f"Bearer {none}"}).status_code, 401)

    def test_unconfigured_sign_in_admits_nobody(self) -> None:
        bare = dataclasses.replace(application.settings, supabase_url="")
        with patch.object(application, "settings", bare):
            self.assertEqual(self.client.get("/api/v1/instances", headers=as_user()).status_code, 401)

    def test_health_stays_public(self) -> None:
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/version").status_code, 200)

    # -- whose tracks -----------------------------------------------------------

    def test_each_user_sees_only_their_own_tracks(self) -> None:
        mine, theirs = self.make(ALICE, "Alice's"), self.make(BOB, "Bob's")
        alice = self.client.get("/api/v1/instances", headers=as_user(ALICE)).json()
        bob = self.client.get("/api/v1/instances", headers=as_user(BOB)).json()
        self.assertEqual([item["id"] for item in alice], [mine])
        self.assertEqual([item["id"] for item in bob], [theirs])
        self.assertNotIn("owner_id", alice[0])

    def test_someone_elses_track_is_not_found_on_every_route(self) -> None:
        mine = self.make(ALICE)
        base = f"/api/v1/instances/{mine}"
        for method, path in (("get", ""), ("get", "/dataset"), ("get", "/dashboard"), ("get", "/live"),
                             ("get", "/messages"), ("get", "/runs"), ("get", "/sources"), ("get", "/graph/parameters"),
                             ("get", "/dataset/export"), ("post", "/duplicate"), ("delete", ""),
                             ("get", "/preferred-sources"), ("get", "/live/stream")):
            with self.subTest(route=f"{method} {path}"):
                response = getattr(self.client, method)(base + path, headers=as_user(BOB))
                self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.client.patch(base, json={"title": "stolen"}, headers=as_user(BOB)).status_code, 404)
        self.assertEqual(self.client.patch(base + "/live", json={"enabled": True}, headers=as_user(BOB)).status_code, 404)
        self.assertEqual(self.client.post(base + "/messages", json={"content": "hi"}, headers=as_user(BOB)).status_code, 404)
        # ... and nothing changed for the owner
        item = self.client.get(base, headers=as_user(ALICE)).json()
        self.assertEqual((item["title"], item["live_enabled"]), ("Track", False))

    def test_duplicates_belong_to_the_person_who_duplicated(self) -> None:
        mine = self.make(ALICE)
        copy = self.client.post(f"/api/v1/instances/{mine}/duplicate", headers=as_user(ALICE)).json()
        self.assertEqual(self.repository.instance_owner(copy["id"]), ALICE)

    def test_webhooks_cache_and_preferred_sources_are_per_user(self) -> None:
        hook = self.client.post("/api/v1/webhooks", json={"url": "https://93.184.216.34/hook", "events": ["run.finished"]},
                                headers=as_user(ALICE))
        self.assertEqual(hook.status_code, 201, hook.text)
        hook_id = hook.json()["id"]
        self.assertEqual(self.client.get("/api/v1/webhooks", headers=as_user(BOB)).json(), [])
        self.assertEqual(self.client.get(f"/api/v1/webhooks/{hook_id}", headers=as_user(BOB)).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/v1/webhooks/{hook_id}", headers=as_user(BOB)).status_code, 404)
        self.assertEqual(len(self.client.get("/api/v1/webhooks", headers=as_user(ALICE)).json()), 1)
        self.client.put("/api/v1/sources/preferred", json={"domains": ["fao.org"]}, headers=as_user(ALICE))
        self.assertEqual(self.client.get("/api/v1/sources/preferred", headers=as_user(ALICE)).json()["domains"], ["fao.org"])
        self.assertEqual(self.client.get("/api/v1/sources/preferred", headers=as_user(BOB)).json()["domains"], [])
        metrics = self.client.get("/api/v1/metrics", headers=as_user(BOB)).json()
        self.assertEqual(metrics["instances"], 0)
        self.assertEqual(self.client.get("/api/v1/metrics").status_code, 401)

    def test_a_run_uses_its_owners_preferred_sources_not_someone_elses(self) -> None:
        mine = self.make(ALICE)
        self.repository.set_preferred(f"user:{ALICE}", ["fao.org"])
        self.repository.set_preferred(f"user:{BOB}", ["who.int"])
        self.assertEqual(self.coordinator._preferred(mine), ["fao.org"])

    # -- old tracks -------------------------------------------------------------

    def test_tracks_from_before_sign_in_go_to_the_configured_owner(self) -> None:
        old = self.repository.create_instance("Old", "prices")["id"]  # no owner, as before sign-in existed
        self.repository.set_preferred("global", ["oecd.org"])
        self.assertEqual(self.client.get("/api/v1/instances", headers=as_user(ALICE)).json(), [])
        self.assertEqual(self.repository.claim_legacy(ALICE), {"instances": 1, "webhooks": 0})
        self.assertEqual([i["id"] for i in self.client.get("/api/v1/instances", headers=as_user(ALICE)).json()], [old])
        self.assertEqual(self.client.get("/api/v1/instances", headers=as_user(BOB)).json(), [])
        self.assertEqual(self.repository.get_preferred(f"user:{ALICE}"), ["oecd.org"])
        self.assertEqual(self.repository.claim_legacy(BOB), {"instances": 0, "webhooks": 0})  # nothing left to claim
        self.assertEqual(self.repository.instance_owner(old), ALICE)

    def test_opening_an_old_database_again_keeps_owners(self) -> None:
        mine = self.make(ALICE)
        reopened = Repository(Path(self.temporary.name) / "auth.db")
        self.assertEqual(reopened.instance_owner(mine), ALICE)

    # -- live socket ------------------------------------------------------------

    def test_the_live_socket_needs_a_token_as_its_first_message(self) -> None:
        mine = self.make(ALICE)
        path = f"/api/v1/instances/{mine}/live/ws"
        with self.client.websocket_connect(path) as socket:
            socket.send_json({"type": "auth", "token": token(ALICE)})
            self.assertEqual(socket.receive_json()["type"], "hello")
        for message in ({"type": "auth", "token": "nope"}, {"type": "auth", "token": token(BOB)}, {"type": "x"}):
            with self.subTest(message=message):
                with self.assertRaises(WebSocketDisconnect):
                    with self.client.websocket_connect(path) as socket:
                        socket.send_json(message)
                        socket.receive_json()

    def test_the_live_stream_answers_only_the_owner(self) -> None:
        mine = self.make(ALICE)
        response = self.client.get(f"/api/v1/instances/{mine}/live/stream")
        self.assertEqual(response.status_code, 401)


class ModeTests(TestCase):
    def test_the_mode_follows_settings(self) -> None:
        base = dataclasses.replace(application.settings, auth="", api_key=None)
        self.assertEqual(base.auth_mode, "none")
        self.assertEqual(dataclasses.replace(base, api_key="k").auth_mode, "api_key")
        self.assertEqual(dataclasses.replace(base, auth="supabase").auth_mode, "supabase")
        self.assertEqual(dataclasses.replace(base, auth="bogus", api_key="k").auth_mode, "api_key")

    def test_without_sign_in_tracks_stay_shared(self) -> None:
        with TemporaryDirectory() as folder:
            repository = Repository(Path(folder) / "open.db")
            plain = dataclasses.replace(application.settings, auth="", api_key=None)
            with patch.object(application, "repository", repository), \
                    patch.object(application, "coordinator", ResearchCoordinator(repository)), \
                    patch.object(application, "settings", plain):
                client = TestClient(application.app)
                created = client.post("/api/v1/instances", json={"title": "A", "goal": "x"})
                self.assertEqual(created.status_code, 201)
                self.assertEqual(len(client.get("/api/v1/instances").json()), 1)


class CorsTests(TestCase):
    def client(self, origins: tuple[str, ...]) -> TestClient:
        target = FastAPI()
        application.install_cors(target, origins)

        @target.get("/ping")
        def ping() -> dict:
            return {"ok": True}

        return TestClient(target)

    def test_a_listed_origin_may_call_with_a_bearer_token(self) -> None:
        client = self.client(("https://awdax.pages.dev",))
        answer = client.options("/ping", headers={
            "Origin": "https://awdax.pages.dev", "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "authorization,content-type"})
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.headers["access-control-allow-origin"], "https://awdax.pages.dev")
        self.assertIn("PATCH", answer.headers["access-control-allow-methods"])
        self.assertNotIn("access-control-allow-credentials", answer.headers)
        real = client.get("/ping", headers={"Origin": "https://awdax.pages.dev"})
        self.assertEqual(real.headers["access-control-allow-origin"], "https://awdax.pages.dev")

    def test_other_origins_get_no_permission(self) -> None:
        client = self.client(("https://awdax.pages.dev",))
        answer = client.options("/ping", headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
        self.assertNotIn("access-control-allow-origin", answer.headers)
        self.assertNotIn("access-control-allow-origin",
                         client.get("/ping", headers={"Origin": "https://evil.example"}).headers)

    def test_no_origins_means_no_cors_at_all(self) -> None:
        client = self.client(())
        self.assertNotIn("access-control-allow-origin",
                         client.get("/ping", headers={"Origin": "https://awdax.pages.dev"}).headers)
