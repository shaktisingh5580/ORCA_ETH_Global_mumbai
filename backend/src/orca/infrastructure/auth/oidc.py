"""Purpose: verify externally issued OIDC access tokens with pinned claims and keys.
Behavior: accept pinned asymmetric algorithms, refresh JWKS on unknown kid, and never mint tokens.
Refs: API sections 2.2,16; main plan P02.2; Phase: P02.
"""

import json
import time
from collections.abc import Callable
from typing import Any, cast
from urllib.parse import urlparse
from urllib.request import urlopen

import jwt
from jwt import InvalidTokenError, PyJWK

from orca.domain.identity import Principal

JwksFetcher = Callable[[], dict[str, Any]]


class OIDCVerifier:
    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_url: str,
        fetcher: JwksFetcher | None = None,
        cache_seconds: int = 300,
    ) -> None:
        if not issuer.startswith("https://") and not issuer.startswith("http://127.0.0.1"):
            raise ValueError("OIDC issuer must use HTTPS except loopback tests")
        parsed = urlparse(jwks_url)
        if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("JWKS URL must use HTTPS except loopback tests")
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.jwks_url = jwks_url
        self.fetcher = fetcher or self._fetch
        self.cache_seconds = cache_seconds
        self._keys: dict[str, PyJWK] = {}
        self._expires_at = 0.0
        self.algorithms = ("ES256", "RS256")

    def _fetch(self) -> dict[str, Any]:
        with urlopen(self.jwks_url, timeout=5) as response:  # noqa: S310 - URL is pinned config
            if (
                response.status != 200
                or int(response.headers.get("Content-Length", "0")) > 1_000_000
            ):
                raise InvalidTokenError("JWKS response rejected")
            return cast(dict[str, Any], json.loads(response.read(1_000_001)))

    def _refresh(self) -> None:
        document = self.fetcher()
        keys = document.get("keys")
        if not isinstance(keys, list) or not keys:
            raise InvalidTokenError("JWKS contains no keys")
        parsed: dict[str, PyJWK] = {}
        for value in keys:
            if (
                isinstance(value, dict)
                and value.get("kid")
                and value.get("alg") in self.algorithms
                and value.get("kty") in {"EC", "RSA"}
            ):
                parsed[str(value["kid"])] = PyJWK.from_dict(value)
        if not parsed:
            raise InvalidTokenError("JWKS contains no supported keys")
        self._keys = parsed
        self._expires_at = time.monotonic() + self.cache_seconds

    def verify(self, token: str) -> Principal:
        header = jwt.get_unverified_header(token)
        if header.get("alg") not in self.algorithms or not isinstance(header.get("kid"), str):
            raise InvalidTokenError("Unsupported token header")
        kid = header["kid"]
        if time.monotonic() >= self._expires_at or kid not in self._keys:
            self._refresh()
        key = self._keys.get(kid)
        if key is None:
            raise InvalidTokenError("Unknown signing key")
        claims = jwt.decode(
            token,
            key.key,
            algorithms=list(self.algorithms),
            audience=self.audience,
            issuer=self.issuer,
            options={"require": ["iss", "aud", "sub", "exp", "nbf", "iat"]},
            leeway=30,
        )
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > 255:
            raise InvalidTokenError("Invalid subject")
        raw_scope = claims.get("scope", "")
        if not isinstance(raw_scope, str):
            raise InvalidTokenError("Invalid scope")
        return Principal(
            subject=subject, issuer=self.issuer, token_scopes=frozenset(raw_scope.split())
        )
