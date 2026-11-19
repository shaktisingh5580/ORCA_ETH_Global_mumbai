"""Purpose: exercise P10 challenge issuance and one-time submission through HTTP.
Behavior: use real PostgreSQL and signature verification with controlled finalized observations.
Refs: main_implementation plan.md P10.3; api design.md §§3.11,4.6; Phase: P10.
"""

import json
import subprocess
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from live_database import prepare_database
from psycopg.types.json import Jsonb

from orca.api.app import create_app
from orca.api.dependencies import current_principal
from orca.api.routers import ens
from orca.config import Settings
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.federation.signing import b64

ARKIV = Path(__file__).resolve().parents[3] / "arkiv"
SIGN_TEST_CHALLENGE = """
import { createPrivateKey, sign } from 'node:crypto';
import { privateKeyToAccount } from 'viem/accounts';
import { bindingTypedData, serviceBindingStatement } from './src/identity-binding.mjs';
let input='';
for await (const chunk of process.stdin) input+=chunk;
const { manifest, key }=JSON.parse(input);
const controller=privateKeyToAccount('0x'+'1'.padStart(64,'0'));
const wallet=privateKeyToAccount('0x'+'2'.padStart(64,'0'));
const service=createPrivateKey({format:'der',type:'pkcs8',key:Buffer.concat([
    Buffer.from('302e020100300506032b657004220420','hex'),Buffer.from(key,'base64url'),
])});
const typed=bindingTypedData(manifest);
console.log(JSON.stringify({
  controller:await controller.signTypedData(typed.ens),
  wallet:await wallet.signTypedData(typed.arkiv),
  service:sign(null,serviceBindingStatement(manifest),service).toString('base64url'),
}));
"""


@pytest.mark.live
@pytest.mark.enable_socket
def test_binding_nonce_consumed_once_and_rechecks_finalized_owner(monkeypatch: pytest.MonkeyPatch):
    admin, runtime, tenant, _, _, _ = prepare_database()
    actor, other = str(uuid4()), str(uuid4())
    agent, binding = uuid4(), uuid4()
    service_key = Ed25519PrivateKey.generate()
    registration = {
        "name": "Scientific provider",
        "key_id": "binding-service",
        "endpoint": "https://provider.example/a2a",
        "public_key": b64(service_key.public_key().public_bytes_raw()),
        "card_digest": "sha256:" + "a" * 64,
    }
    with psycopg.connect(admin) as connection:
        organization = connection.execute(
            "SELECT id FROM iam.organizations WHERE tenant_id=%s", (tenant,)
        ).fetchone()[0]
        for subject in (actor, other):
            connection.execute(
                "INSERT INTO iam.memberships(id,tenant_id,subject,role) VALUES(%s,%s,%s,'org_owner')",
                (uuid4(), tenant, subject),
            )
        connection.execute(
            "INSERT INTO federation.registrations "
            "(id,tenant_id,organization_id,created_by,idempotency_key,request_digest,binding_version,state) "
            "VALUES(%s,%s,%s,%s,'binding-test',%s,%s,'active')",
            (agent, tenant, organization, actor, registration["card_digest"], binding),
        )
        connection.execute(
            "INSERT INTO federation.registration_versions "
            "(id,tenant_id,organization_id,agent_id,registration) VALUES(%s,%s,%s,%s,%s)",
            (binding, tenant, organization, agent, Jsonb(registration)),
        )
    controller = "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf"
    wallet = "0x2b5ad5c4795c026514f8317c7a215e218dccd6cf"
    name = "analyst.orca-research-lab.eth"
    owner = controller

    def observe(query):
        assert query == name
        return {
            "name": name,
            "status": "NAME_OBSERVED_NOT_AUTHORIZED",
            "chain_id": 11155111,
            "block_number": "123",
            "block_hash": "0x" + "c" * 64,
            "hierarchy": [{"owner": owner, "resolver": "0x" + "1" * 40}],
            "records": {
                "org.orca.endpoint": registration["endpoint"],
                "org.orca.cardDigest": registration["card_digest"],
                "org.orca.serviceKey": registration["public_key"],
                "org.orca.arkivWallet": wallet,
            },
        }

    monkeypatch.setattr(ens, "inspect_name", observe)
    db = create_runtime_engine(runtime)
    app = create_app(
        Settings(monitoring_token="0123456789abcdefghijklmnopqrstuvwxyz", environment="test")
    )
    app.state.runtime_engine = db
    subject = actor
    app.dependency_overrides[current_principal] = lambda: Principal(
        subject, "test", ROLE_SCOPES["org_owner"]
    )
    try:
        with TestClient(app) as client:
            path = "/v1/identity/agent-bindings"
            challenge = client.post(
                path + "/challenges",
                json={
                    "organization_id": str(organization),
                    "agent_id": str(agent),
                    "expected_version": 1,
                    "name": name,
                    "ens_chain_id": 11155111,
                    "arkiv_wallet": wallet,
                },
            )
            assert challenge.status_code == 201, challenge.text
            issued = challenge.json()
            result = subprocess.run(
                ["node", "--input-type=module", "-e", SIGN_TEST_CHALLENGE],
                cwd=ARKIV,
                input=json.dumps(
                    {
                        "manifest": issued["manifest"],
                        "key": b64(service_key.private_bytes_raw()),
                    }
                ),
                capture_output=True,
                encoding="utf-8",
                timeout=30,
                check=True,
            )
            signatures = json.loads(result.stdout)
            submission = {
                "organization_id": str(organization),
                "challenge_id": issued["id"],
                **signatures,
            }
            subject = other
            assert client.post(path, json=submission).status_code == 404
            subject = actor
            assert (
                client.post(
                    path, json={**submission, "controller": signatures["wallet"]}
                ).status_code
                == 403
            )
            owner = "0x" + "2" * 40
            assert client.post(path, json=submission).status_code == 409
            owner = controller
            valid = client.post(path, json=submission)
            assert valid.status_code == 201, valid.text
            assert valid.json()["authority_verified"] is False
            assert valid.json()["research_dispatch_enabled"] is False
            assert valid.json()["observed_block"] == 123
            assert client.post(path, json=submission).status_code == 404
            evidence = f"{path}/{issued['id']}?organization_id={organization}"
            assert client.get(evidence).json() == valid.json()
            subject = other
            assert client.get(evidence).status_code == 404
    finally:
        db.dispose()
