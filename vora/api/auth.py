"""Who is calling: verifying a signed-in user's Supabase access token.

The web app signs users in with Supabase (Google) and sends each API call with
``Authorization: Bearer <access token>``. The token is a signed JWT; this module
checks the signature against the project's public keys (JWKS), its issuer,
audience and expiry, and returns the user id (``sub``) that owns the tracks.
Nothing here holds a secret: the public keys are fetched from the project URL.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import jwt
from jwt import PyJWKClient

logger = logging.getLogger("vora.auth")

ASYMMETRIC = ("ES256", "RS256", "EdDSA")


@dataclass(frozen=True, slots=True)
class User:
    id: str
    email: str | None = None


# The caller of the request being handled; set by the `authorized` dependency.
current_user: ContextVar[User | None] = ContextVar("vora_current_user", default=None)


class AuthError(Exception):
    """The token is missing or not acceptable; the message is safe to show."""


def bearer_token(header: str | None) -> str:
    if not header:
        raise AuthError("Sign in required")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError("Sign in required")
    return token.strip()


class SupabaseVerifier:
    """Verifies Supabase access tokens. One instance is shared; the key sets are cached."""

    def __init__(self) -> None:
        self._clients: dict[str, PyJWKClient] = {}

    def signing_key(self, token: str, supabase_url: str) -> Any:
        """The public key that should have signed ``token`` (fetches the JWKS, cached)."""
        client = self._clients.get(supabase_url)
        if client is None:
            client = self._clients[supabase_url] = PyJWKClient(
                f"{supabase_url}/auth/v1/.well-known/jwks.json", cache_keys=True, lifespan=600, timeout=10)
        return client.get_signing_key_from_jwt(token).key

    def verify(self, token: str, settings: Any) -> User:
        if not settings.supabase_url:
            raise AuthError("Sign-in is not configured on this server")
        issuer = f"{settings.supabase_url}/auth/v1"
        try:
            algorithm = jwt.get_unverified_header(token).get("alg", "")
            if algorithm == "HS256":
                if not settings.supabase_jwt_secret:
                    raise AuthError("Invalid token")
                key: Any = settings.supabase_jwt_secret
            elif algorithm in ASYMMETRIC:
                key = self.signing_key(token, settings.supabase_url)
            else:
                raise AuthError("Invalid token")
            claims = jwt.decode(
                token, key, algorithms=[algorithm], audience=settings.supabase_jwt_audience,
                issuer=issuer, leeway=10, options={"require": ["exp", "sub", "iss", "aud"]})
        except AuthError:
            raise
        except jwt.ExpiredSignatureError:
            raise AuthError("Session expired") from None
        except jwt.PyJWTError as exc:
            logger.info("Rejected a token: %s", type(exc).__name__)
            raise AuthError("Invalid token") from None
        except Exception:  # noqa: BLE001 - key fetch failed (network); do not admit the caller
            logger.warning("Could not check a token", exc_info=True)
            raise AuthError("Sign-in could not be verified, try again") from None
        return User(id=str(claims["sub"]), email=claims.get("email"))


verifier = SupabaseVerifier()
