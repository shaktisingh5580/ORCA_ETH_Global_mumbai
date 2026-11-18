"""Purpose: identify the P09 onboarding coordinator independently of the H04 prototype.
Behavior: load a public API identity and verify the worker-only Ed25519 private key matches it.
Refs: main_implementation plan.md P09.1-P09.3; api design.md §§3.10,4.6; Phase: P09.
"""

from dataclasses import dataclass
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orca.config import Settings
from orca.infrastructure.federation.signing import b64, unb64


@dataclass(frozen=True)
class CoordinatorIdentity:
    id: UUID
    key_id: str
    public_key: str

    def public_descriptor(self) -> dict[str, str]:
        return {"id": str(self.id), "key_id": self.key_id, "public_key": self.public_key}


@dataclass(frozen=True)
class FederationCoordinator(CoordinatorIdentity):
    private_key: str


def load_public_coordinator(settings: Settings) -> CoordinatorIdentity:
    if not (
        settings.agent_onboarding_enabled
        and settings.federation_coordinator_id
        and settings.federation_coordinator_key_id
        and settings.federation_coordinator_public_key
    ):
        raise ValueError("Federated onboarding coordinator is not configured")
    public_key = settings.federation_coordinator_public_key
    if len(unb64(public_key)) != 32:
        raise ValueError("Federation coordinator public key must decode to 32 bytes")
    return CoordinatorIdentity(
        settings.federation_coordinator_id,
        settings.federation_coordinator_key_id,
        public_key,
    )


def load_coordinator(settings: Settings) -> FederationCoordinator:
    identity = load_public_coordinator(settings)
    if settings.federation_coordinator_private_key is None:
        raise ValueError("Federation worker requires a private signing key")
    private_key = settings.federation_coordinator_private_key.get_secret_value()
    raw = unb64(private_key)
    if len(raw) != 32:
        raise ValueError("Federation coordinator key must decode to 32 bytes")
    public_key = b64(
        Ed25519PrivateKey.from_private_bytes(raw)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    if public_key != identity.public_key:
        raise ValueError("Federation worker key differs from configured public identity")
    return FederationCoordinator(identity.id, identity.key_id, identity.public_key, private_key)
