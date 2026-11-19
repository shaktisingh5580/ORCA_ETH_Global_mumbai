"""Purpose: exercise the P13 provider release and cross-organization boundaries.
Behavior: prove exact grant matching, local revocation and rejection of public raw-data fields.
Refs: main_implementation plan.md P13.2; api design.md §17.1. Phase: P13.
"""

import json
import subprocess
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orca.application.federation import ResearchGrant, sign_grant
from orca.contracts.remote_dispatch import ProviderTemperatureSummary
from orca.infrastructure.federation.provider_research import approved_inputs
from orca.infrastructure.federation.signing import b64, digest
from orca.institution_provider import compute


@pytest.fixture
def approval(tmp_path):
    grant = ResearchGrant(
        audience_agent_id=uuid4(),
        binding_version=uuid4(),
        organization_id=uuid4(),
        project_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
        capability_id="temperature_summary_v1",
        method_id="arithmetic_mean/1.0",
        approved_plan_digest="sha256:" + "a" * 64,
        dataset_ids=[uuid4()],
        max_cost_usd=Decimal("1"),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
    )
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
    config = {
        "coordinator": {"public_key": public, "key_id": "test-coordinator"},
        "agent_id": str(grant.audience_agent_id),
        "research_binding_version": str(grant.binding_version),
        "research_plans": {
            grant.approved_plan_digest: {
                "organization_id": str(grant.organization_id),
                "project_id": str(grant.project_id),
                "dataset_ids": [str(item) for item in grant.dataset_ids],
                "values": [24, 27, 29],
                "unit": "C",
            }
        },
    }
    document = {
        "schema": "orca.provider-release.v1",
        "plans": {
            grant.approved_plan_digest: {
                "rights_record_digest": "sha256:" + "b" * 64,
                "min_samples": 3,
            }
        },
    }
    policy = tmp_path / "research-policy.json"
    policy.write_text(json.dumps(document), encoding="utf-8")

    def request(selected):
        signed = sign_grant(selected, private, "test-coordinator")
        data = {
            "schema_version": "orca.scientific_task/1.0",
            "grant": signed.model_dump(mode="json"),
        }
        auth = {
            "request_digest": digest(data),
            "grant_digest": signed.grant_digest,
            "jti": str(selected.task_id),
        }
        return approved_inputs(data, auth, config, policy)

    return grant, request, policy, document


def test_provider_keeps_private_dataset_offchain(approval):
    grant, request, _, _ = approval
    _, local = request(grant)
    aggregate = {**compute(local), "source_mode": "operator_approved_values"}
    assert ProviderTemperatureSummary.model_validate(aggregate).count == 3
    assert "values" not in aggregate
    with pytest.raises(ValueError):
        ProviderTemperatureSummary.model_validate({**aggregate, "values": local["values"]})
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            """
import {validatePublicAttributes} from './arkiv/src/public-schema.mjs';
const record = {taskId:'12345678-1234-4567-8123-0123456789ab', artifactDigest:'sha256:'+'a'.repeat(64),
  methodId:'arithmetic_mean/1.0', status:'inconclusive'};
validatePublicAttributes('result',record);
try { validatePublicAttributes('result',{...record,values:[24,27,29]}); }
catch { process.exit(0); }
process.exit(1);
""",
        ],
        cwd=root,
        check=False,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_permission_revocation_stops_new_task(approval):
    grant, request, policy, document = approval
    request(grant)
    policy.write_text(
        json.dumps(
            {
                **document,
                "plans": {
                    grant.approved_plan_digest: {
                        **document["plans"][grant.approved_plan_digest],
                        "min_samples": 4,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="release is not approved"):
        request(grant)
    policy.write_text(json.dumps({**document, "plans": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="release is not approved"):
        request(grant.model_copy(update={"task_id": uuid4()}))


def test_cannot_cross_org_evidence(approval):
    grant, request, _, _ = approval
    for change in (
        {"organization_id": uuid4()},
        {"project_id": uuid4()},
        {"dataset_ids": [uuid4()]},
    ):
        with pytest.raises(ValueError, match="operator-approved"):
            request(grant.model_copy(update=change))
