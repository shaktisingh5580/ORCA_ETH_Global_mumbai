"""Purpose: reject forged or overbroad P09 remote research grants.
Behavior: test exact binding, data, budget, lifetime and detached signature checks.
Refs: main_implementation plan.md P09.3; api design.md §§2.2,17.1; Phase: P09.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orca.application.federation import (
    ResearchGrant,
    sign_grant,
    validate_scope,
    verify_grant,
)
from orca.infrastructure.federation.signing import b64


def test_signed_grant_scope() -> None:
    now = datetime.now(timezone.utc)
    dataset = uuid4()
    grant = ResearchGrant(
        audience_agent_id=uuid4(),
        binding_version=uuid4(),
        organization_id=uuid4(),
        project_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
        capability_id="sst_reproduction",
        approved_plan_digest="sha256:" + "a" * 64,
        method_id="sst.seasonal_anomaly/1.0",
        dataset_ids=[dataset],
        max_cost_usd=Decimal("2.00"),
        expires_at=now + timedelta(minutes=2),
    )
    scope = {
        "agent_id": grant.audience_agent_id,
        "binding_version": grant.binding_version,
        "organization_id": grant.organization_id,
        "project_id": grant.project_id,
        "run_id": grant.run_id,
        "task_id": grant.task_id,
        "capability_id": grant.capability_id,
        "approved_plan_digest": grant.approved_plan_digest,
        "approved_method_id": grant.method_id,
        "permitted_dataset_ids": {dataset},
        "available_budget_usd": Decimal("2.00"),
        "service_eligible": True,
        "research_dispatch_enabled": True,
        "now": now,
    }
    validate_scope(grant, **scope)
    for changed in (
        {"agent_id": uuid4()},
        {"binding_version": uuid4()},
        {"organization_id": uuid4()},
        {"project_id": uuid4()},
        {"run_id": uuid4()},
        {"task_id": uuid4()},
        {"approved_plan_digest": "sha256:" + "b" * 64},
        {"approved_method_id": "unapproved.method"},
        {"permitted_dataset_ids": set()},
        {"available_budget_usd": Decimal("1.00")},
        {"research_dispatch_enabled": False},
    ):
        with pytest.raises(PermissionError):
            validate_scope(grant, **{**scope, **changed})
    with pytest.raises(PermissionError):
        validate_scope(grant.model_copy(update={"expires_at": now}), **scope)

    key = Ed25519PrivateKey.generate()
    private = b64(
        key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    )
    public = b64(
        key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    signed = sign_grant(grant, private, "coordinator-v1")
    assert verify_grant(signed, public, "coordinator-v1") == grant
    with pytest.raises(ValueError):
        verify_grant(
            signed.model_copy(update={"grant": grant.model_copy(update={"run_id": uuid4()})}),
            public,
            "coordinator-v1",
        )
    with pytest.raises(ValueError):
        verify_grant(signed, public, "other-key")
