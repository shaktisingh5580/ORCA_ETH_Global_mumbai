"""Purpose: authenticate native Supabase user sessions without a custom token scope hook.
Behavior: verify with the pinned Auth server, validate claims and intersect capabilities with DB roles.
Refs: Supabase JWT claims/getUser docs; accelerated demo decision; Phase: accelerated P02 integration.
"""

import json
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import UUID

import jwt
from jwt import InvalidTokenError

from orca.domain.identity import ROLE_SCOPES, Principal


class SupabaseVerifier:
    def __init__(self, issuer: str, anon_key: str):
        parsed = urlsplit(issuer)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".supabase.co")
            or parsed.path.rstrip("/") != "/auth/v1"
            or parsed.username
            or parsed.query
            or parsed.fragment
            or parsed.port not in {None, 443}
        ):
            raise ValueError("Supabase verifier requires a pinned project Auth issuer")
        self.issuer, self.anon_key = issuer.rstrip("/"), anon_key

    def verify(self, token: str) -> Principal:
        if len(token) > 16_384:
            raise InvalidTokenError("Oversized access token")
        request = Request(
            self.issuer + "/user",
            headers={"Authorization": "Bearer " + token, "apikey": self.anon_key},
        )
        try:
            with urlopen(request, timeout=10) as response:
                raw = response.read(65_537)
                if response.status != 200 or len(raw) > 65_536:
                    raise InvalidTokenError("Auth response rejected")
                user: Any = json.loads(raw)
        except (URLError, ValueError) as exc:
            raise InvalidTokenError("Supabase session rejected") from exc
        # The Auth server has verified this exact token's signature, including legacy projects.
        claims = jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
                "require": ["iss", "aud", "sub", "exp", "iat"],
            },
            issuer=self.issuer,
            audience="authenticated",
            leeway=30,
        )
        subject = claims.get("sub")
        if (
            not isinstance(subject, str)
            or not isinstance(user, dict)
            or user.get("id") != subject
            or claims.get("role") != "authenticated"
        ):
            raise InvalidTokenError("Authenticated user session required")
        try:
            UUID(subject)
        except ValueError as exc:
            raise InvalidTokenError("Invalid Supabase subject") from exc
        scope = claims.get("scope")
        if scope is not None and not isinstance(scope, str):
            raise InvalidTokenError("Invalid scope")
        capabilities = (
            frozenset(scope.split())
            if scope is not None
            else frozenset().union(*ROLE_SCOPES.values())
        )
        # Membership and operation role checks still run against the private database on each request.
        return Principal(
            subject=subject,
            issuer=self.issuer,
            token_scopes=capabilities,
            verified_email=user["email"].casefold()
            if user.get("email_confirmed_at")
            and isinstance(user.get("email"), str)
            and len(user["email"]) <= 254
            else None,
        )
