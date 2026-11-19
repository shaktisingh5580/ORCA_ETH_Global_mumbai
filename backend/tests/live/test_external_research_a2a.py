"""Purpose: exercise P09 against an operator-approved, separately operated HTTPS provider.
Behavior: send a sanctioned task over pinned TLS and verify readback and cancellation.
Refs: main_implementation plan.md P09.2–P09.3; api design.md §17.1; Phase: P09.
"""

import asyncio
import os
from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest

from orca.application.federation import ResearchDispatchScope, SignedResearchGrant
from orca.infrastructure.federation.coordinator import FederationCoordinator
from orca.infrastructure.federation.research_a2a_client import (
    cancel_research_task,
    dispatch_research_task,
    read_research_result,
)
from orca.infrastructure.federation.signing import strict_json

pytestmark = [pytest.mark.live, pytest.mark.enable_socket]


@pytest.fixture
def external_provider():
    path = os.getenv("ORCA_TEST_EXTERNAL_A2A_BUNDLE")
    if not path:
        pytest.skip("Set ORCA_TEST_EXTERNAL_A2A_BUNDLE for a sanctioned external HTTPS provider")
    with Path(path).open("rb") as handle:
        raw = handle.read(65_537)
    if len(raw) > 65_536:
        pytest.fail("Live provider bundle exceeds its size limit", pytrace=False)
    bundle = strict_json(raw)
    identity = bundle["coordinator"]
    coordinator = FederationCoordinator(
        UUID(identity["id"]), identity["key_id"], identity["public_key"], identity["private_key"]
    )
    return bundle, coordinator


def test_official_sdk_client_server_exchange(external_provider, record_property):
    bundle, coordinator = external_provider
    signed = SignedResearchGrant.model_validate(bundle["signed_grant"])
    scope = ResearchDispatchScope.model_validate(bundle["scope"])
    agent = dict(bundle["agent"])
    for field in ("id", "binding_version", "organization_id"):
        agent[field] = UUID(agent[field])
    agent["qualification_expires_at"] = datetime.fromisoformat(agent["qualification_expires_at"])
    if signed.grant.endpoint != agent["registration"]["endpoint"]:
        pytest.fail("Verify live research against an explicitly pinned endpoint", pytrace=False)

    async def exercise():
        async with asyncio.timeout(110):
            result = await dispatch_research_task(
                signed,
                agent,
                scope,
                coordinator.id,
                coordinator.public_key,
                coordinator.private_key,
                coordinator.key_id,
            )
            recovered = await read_research_result(signed, agent["registration"], coordinator, None)
            assert recovered == result
            return result

    try:
        result = asyncio.run(exercise())
    except Exception as error:
        pytest.fail(f"External dispatch or readback failed ({type(error).__name__})", pytrace=False)
    record_property("artifact_digest", result["artifact_digest"])
    record_property("grant_digest", signed.grant_digest)


def test_cancel_and_reconnect(external_provider, record_property):
    bundle, coordinator = external_provider
    target = bundle.get("cancellation")
    if not target:
        pytest.skip("Supply a separate authorized A2A task that is still cancellable")
    signed = SignedResearchGrant.model_validate(target["signed_grant"])
    registration = bundle["agent"]["registration"]
    if signed.grant.endpoint != registration["endpoint"]:
        pytest.fail("Cancellation fixture is bound to another endpoint", pytrace=False)

    async def exercise():
        async with asyncio.timeout(110):
            reference = (target["remote_task_id"], target["remote_context_id"])
            return await cancel_research_task(signed, registration, coordinator, reference)

    try:
        confirmed = asyncio.run(exercise())
    except Exception as error:
        pytest.fail(f"External cancellation failed ({type(error).__name__})", pytrace=False)
    assert confirmed, "Provider did not confirm cancellation on readback"
    record_property("grant_digest", signed.grant_digest)
