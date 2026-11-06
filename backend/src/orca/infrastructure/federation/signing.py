"""Purpose: authenticate canonical agent statements and reject context/signature substitution.
Behavior: sign RFC8785 payload digests with detached Ed25519 domain/audience statements.
Refs: docs/02_agentic_architecture.md signing contract; H04 decision; Phase: H04.
"""

import base64
import hashlib
import json
from typing import Any

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from orca.contracts.network import SignedEnvelope

DOMAIN = "orca.agent-coordination/hackathon-v1"
CHAIN_ID = "7794095"


def canonical(value: Any) -> bytes:
    return rfc8785.dumps(value)


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def strict_json(raw: bytes | str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in items:
            if key in output:
                raise ValueError("duplicate JSON key")
            output[key] = value
        return output

    def reject(value: str) -> None:
        raise ValueError("non-finite JSON number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def unb64(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def statement(payload: dict[str, Any], content_digest: str, key_id: str) -> dict[str, Any]:
    return {
        "schema_version": "orca.detached-agent-signature/1.0",
        "domain": DOMAIN,
        "audience": payload["audience"],
        "task_id": payload["task_id"],
        "assignment_generation": payload["assignment_generation"],
        "binding_version": payload["binding_version"],
        "content_digest": content_digest,
        "key_id": key_id,
    }


def sign(payload: dict[str, Any], private_key: str, key_id: str) -> SignedEnvelope:
    content_digest = digest(payload)
    signature = Ed25519PrivateKey.from_private_bytes(unb64(private_key)).sign(
        canonical(statement(payload, content_digest, key_id))
    )
    return SignedEnvelope(
        payload=payload, content_digest=content_digest, key_id=key_id, signature=b64(signature)
    )


def verify(envelope: SignedEnvelope, public_key: str, key_id: str, audience: str) -> None:
    payload = envelope.payload
    if (
        envelope.key_id != key_id
        or payload.get("domain") != DOMAIN
        or payload.get("audience") != audience
        or payload.get("chain_id") != CHAIN_ID
        or digest(payload) != envelope.content_digest
    ):
        raise ValueError("signature context or payload mismatch")
    try:
        Ed25519PublicKey.from_public_bytes(unb64(public_key)).verify(
            unb64(envelope.signature),
            canonical(statement(payload, envelope.content_digest, key_id)),
        )
    except (InvalidSignature, ValueError) as error:
        raise ValueError("invalid agent signature") from error
