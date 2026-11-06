"""Purpose: prove OIDC claim, signature, key rotation and role-scope enforcement.
Behavior: use an in-process local issuer key; never rely on a dummy production principal.
Refs: main plan P02.2; API sections 2.2,16; Phase: P02.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt import InvalidTokenError

from orca.domain.identity import Principal
from orca.infrastructure.auth.oidc import OIDCVerifier

ISSUER = "http://127.0.0.1:8099"
AUDIENCE = "orca-api"


@pytest.fixture(scope="module")
def issuer() -> tuple[OIDCVerifier, Any, dict[str, Any]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json_web_key(key.public_key(), "local-key")
    document = {"keys": [jwk]}
    return OIDCVerifier(ISSUER, AUDIENCE, ISSUER + "/jwks", lambda: document), key, document


def json_web_key(key: Any, kid: str, algorithm: str = "RS256") -> dict[str, Any]:
    converter = jwt.algorithms.ECAlgorithm if algorithm == "ES256" else jwt.algorithms.RSAAlgorithm
    value = converter.to_jwk(key, as_dict=True)
    value.update({"kid": kid, "use": "sig", "alg": algorithm})
    return value


def token(key: Any, **updates: Any) -> str:
    now = datetime.now(timezone.utc)
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-123",
        "iat": now,
        "nbf": now - timedelta(seconds=1),
        "exp": now + timedelta(minutes=5),
        "scope": "org:read org:write project:read project:write",
    }
    claims.update(updates)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "local-key"})


def test_valid_local_issuer_token(issuer):
    verifier, key, _ = issuer
    principal = verifier.verify(token(key))
    assert principal.subject == "user-123"
    assert principal.issuer == ISSUER
    assert "project:read" in principal.token_scopes


def test_valid_supabase_es256_token():
    key = ec.generate_private_key(ec.SECP256R1())
    document = {"keys": [json_web_key(key.public_key(), "supabase-key", "ES256")]}
    verifier = OIDCVerifier(ISSUER, AUDIENCE, ISSUER + "/jwks", lambda: document)
    now = datetime.now(timezone.utc)
    value = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "supabase-user",
            "iat": now,
            "nbf": now - timedelta(seconds=1),
            "exp": now + timedelta(minutes=5),
            "scope": "project:read",
        },
        key,
        algorithm="ES256",
        headers={"kid": "supabase-key"},
    )
    assert verifier.verify(value).subject == "supabase-user"


def test_wrong_issuer_audience_signature_kid(issuer):
    verifier, key, _ = issuer
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cases = [
        token(key, iss="https://wrong.example"),
        token(key, aud="wrong"),
        token(other),
        jwt.encode({"sub": "x"}, key, algorithm="RS256", headers={"kid": "absent"}),
    ]
    for value in cases:
        with pytest.raises(InvalidTokenError):
            verifier.verify(value)
    forged_none = jwt.encode(
        {"iss": ISSUER}, key, algorithm="RS256", headers={"kid": "local-key", "alg": "RS256"}
    )
    with pytest.raises(InvalidTokenError):
        verifier.verify(forged_none)


def test_expired_and_nbf(issuer):
    verifier, key, _ = issuer
    now = datetime.now(timezone.utc)
    for value in (
        token(key, exp=now - timedelta(minutes=2)),
        token(key, nbf=now + timedelta(minutes=2)),
    ):
        with pytest.raises(InvalidTokenError):
            verifier.verify(value)


def test_role_scope_downgrade():
    principal = Principal(
        "user", ISSUER, frozenset({"org:read", "org:write", "project:read", "platform:admin"})
    )
    assert principal.effective_scopes({"viewer"}) == {"org:read", "project:read"}
    with pytest.raises(PermissionError):
        principal.require("org:write", {"viewer"})
    assert "platform:admin" not in principal.effective_scopes({"org_owner"})


def test_unknown_workspace_denied():
    principal = Principal("user", ISSUER, frozenset({"org:read", "project:read"}))
    with pytest.raises(PermissionError):
        principal.require("project:read", set())
