"""Purpose: issue and consume P10 one-time cross-chain signature challenges.
Behavior: bind evidence to current agent registration and finalized ENS records without enabling trust.
Refs: main_implementation plan.md P10.3; api design.md §§3.11,4.6; Phase: P10.
"""

import json
import re
import secrets
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import text

from orca.application.agent_onboarding import Onboarding
from orca.contracts.ens import (
    CreateIdentityBinding,
    IdentityBindingChallenge,
    IdentityBindingEvidence,
)


def check_finalized_identity(
    observation: dict[str, Any], name: str, registration: dict[str, Any], wallet: str
) -> tuple[str, str, int, str]:
    hierarchy = observation.get("hierarchy")
    if (
        observation.get("status") != "NAME_OBSERVED_NOT_AUTHORIZED"
        or observation.get("chain_id") != 11155111
        or observation.get("name") != name
        or not isinstance(hierarchy, list)
        or not hierarchy
        or not isinstance(hierarchy[-1], dict)
    ):
        raise ValueError("An active finalized Sepolia name is required")
    leaf = hierarchy[-1]
    controller, resolver = leaf.get("owner"), leaf.get("resolver")
    if not all(
        isinstance(address, str)
        and re.fullmatch(r"0x[0-9a-fA-F]{40}", address)
        and int(address, 16) != 0
        for address in (controller, resolver, wallet)
    ):
        raise ValueError(
            "Finalized controller, resolver and Arkiv wallet must be nonzero addresses"
        )
    assert isinstance(controller, str) and isinstance(resolver, str)
    records = observation.get("records")
    expected = {
        "org.orca.endpoint": registration["endpoint"],
        "org.orca.cardDigest": registration["card_digest"],
        "org.orca.serviceKey": registration["public_key"],
    }
    if not isinstance(records, dict) or any(
        records.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("Finalized ENS records differ from the registered service")
    recorded_wallet = records.get("org.orca.arkivWallet")
    if not isinstance(recorded_wallet, str) or recorded_wallet.lower() != wallet.lower():
        raise ValueError("Finalized ENS wallet record differs")
    block_hash = observation.get("block_hash")
    block_number = observation.get("block_number")
    if (
        not isinstance(block_hash, str)
        or not re.fullmatch(r"0x[0-9a-f]{64}", block_hash)
        or not isinstance(block_number, str)
        or not re.fullmatch(r"[0-9]{1,15}", block_number)
        or int(block_number) < 1
    ):
        raise ValueError("Finalized observation lacks valid block evidence")
    return controller.lower(), resolver.lower(), int(block_number), block_hash


class IdentityBindings:
    def __init__(self, onboarding: Onboarding) -> None:
        self.onboarding = onboarding
        self.session = onboarding.session

    def create(
        self,
        request: CreateIdentityBinding,
        observation: dict[str, Any],
    ) -> IdentityBindingChallenge:
        agent = self.onboarding.expected(request.agent_id, request.expected_version)
        if agent["state"] != "active":
            raise ValueError("Activate the current A2A registration first")
        registration = agent["registration"]
        controller, resolver, number, block_hash = check_finalized_identity(
            observation, request.name, registration, request.arkiv_wallet
        )
        endpoint = urlsplit(registration["endpoint"])
        host = endpoint.hostname
        if endpoint.scheme != "https" or not host or endpoint.port not in (443, None):
            raise ValueError("Registered endpoint has an unsupported origin")
        host = f"[{host}]" if ":" in host else host.encode("idna").decode("ascii")
        now = int(
            self.session.execute(
                text("SELECT floor(extract(epoch from clock_timestamp()))")
            ).scalar_one()
        )
        identifier = uuid4()
        manifest = {
            "organizationId": str(request.organization_id),
            "agentId": str(request.agent_id),
            "audience": "orca.agent-binding",
            "ensName": request.name,
            "ensResolver": resolver,
            "ensController": controller,
            "ensObservedBlock": number,
            "ensBlockHash": block_hash,
            "arkivWallet": request.arkiv_wallet.lower(),
            "serviceKeyId": registration["key_id"],
            "servicePublicKey": registration["public_key"].rstrip("="),
            "endpointOrigin": f"https://{host}",
            "cardDigest": "0x" + registration["card_digest"].removeprefix("sha256:"),
            "credentialDigest": "0x" + "0" * 64,
            "scopes": ["identity:read"],
            "nonce": "0x" + secrets.token_hex(32),
            "issuedAt": now,
            "expiresAt": now + 300,
            "revocationEpoch": agent["version"],
        }
        self.session.execute(
            text("""INSERT INTO federation.identity_binding_challenges
              (id,tenant_id,organization_id,agent_id,binding_version,agent_version,actor,manifest,expires_at)
              VALUES(:id,:t,:o,:agent,:binding,:version,:actor,CAST(:manifest AS jsonb),to_timestamp(:expiry))"""),
            {
                **self.onboarding.params(True),
                "id": identifier,
                "agent": request.agent_id,
                "binding": agent["binding_version"],
                "version": agent["version"],
                "actor": self.onboarding.principal.subject,
                "manifest": json.dumps(manifest),
                "expiry": manifest["expiresAt"],
            },
        )
        return IdentityBindingChallenge(id=identifier, manifest=manifest)

    def pending(self, identifier: UUID) -> dict[str, Any]:
        row = (
            self.session.execute(
                text("""SELECT agent_id,agent_version,binding_version,manifest FROM federation.identity_binding_challenges
              WHERE tenant_id=:t AND organization_id=:o AND id=:id AND actor=:actor
                AND consumed_at IS NULL AND expires_at>clock_timestamp()"""),
                {
                    **self.onboarding.params(True),
                    "id": identifier,
                    "actor": self.onboarding.principal.subject,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("No active binding challenge")
        return dict(row)

    def evidence(self, identifier: UUID) -> IdentityBindingEvidence:
        payload = self.session.execute(
            text("""SELECT evidence FROM federation.identity_binding_challenges
              WHERE tenant_id=:t AND organization_id=:o AND id=:id AND actor=:actor
                AND consumed_at IS NOT NULL"""),
            {
                **self.onboarding.params(True),
                "id": identifier,
                "actor": self.onboarding.principal.subject,
            },
        ).scalar_one_or_none()
        if payload is None:
            raise LookupError("No binding evidence was found")
        return IdentityBindingEvidence.model_validate(payload)

    def consume(
        self,
        identifier: UUID,
        observation: dict[str, Any],
        signatures: dict[str, Any],
    ) -> IdentityBindingEvidence:
        row = self.pending(identifier)
        manifest = row["manifest"]
        agent = self.onboarding.expected(row["agent_id"], row["agent_version"])
        if agent["state"] != "active" or agent["binding_version"] != row["binding_version"]:
            raise ValueError("Agent binding changed before acceptance")
        owner, resolver, number, block_hash = check_finalized_identity(
            observation, manifest["ensName"], agent["registration"], manifest["arkivWallet"]
        )
        if (
            owner != manifest["ensController"]
            or resolver != manifest["ensResolver"]
            or number < manifest["ensObservedBlock"]
            or (number == manifest["ensObservedBlock"] and block_hash != manifest["ensBlockHash"])
        ):
            raise ValueError("Finalized ENS identity changed before acceptance")
        evidence = IdentityBindingEvidence(
            challenge_id=identifier,
            agent_id=row["agent_id"],
            ens_digest=signatures["ens_digest"],
            arkiv_digest=signatures["arkiv_digest"],
            observed_block=number,
            block_hash=block_hash,
        )
        accepted = self.session.execute(
            text("""UPDATE federation.identity_binding_challenges
              SET consumed_at=clock_timestamp(),evidence=CAST(:evidence AS jsonb)
              WHERE tenant_id=:t AND organization_id=:o AND id=:id AND actor=:actor
                AND consumed_at IS NULL AND expires_at>clock_timestamp() RETURNING id"""),
            {
                **self.onboarding.params(True),
                "id": identifier,
                "actor": self.onboarding.principal.subject,
                "evidence": evidence.model_dump_json(),
            },
        ).scalar_one_or_none()
        if accepted is None:
            raise LookupError("No active binding challenge")
        return evidence
