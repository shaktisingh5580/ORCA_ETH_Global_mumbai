"""Purpose: prove tenant-owned onboarding, immutable proof scope and real A2A qualification.
Behavior: run real SDK/provider ASGI with isolated Postgres only; no cloud or chain mutations.
Refs: institutional phase F02.1-F02.5; main_implementation plan.md P09; Phase: F02/P09 test tooling.
"""

import asyncio
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from live_database import prepare_database
from sqlalchemy import text

from orca.application.agent_onboarding import Onboarding
from orca.application.projects import IdempotencyConflict, StaleVersion
from orca.contracts.agents import AgentDraft, AgentProof
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.federation.qualification import qualify_async
from orca.infrastructure.federation.signing import b64, canonical, digest
from orca.institution_provider import card, create_provider


def keys():
    key = Ed25519PrivateKey.generate()
    return (
        key,
        b64(
            key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        ),
        b64(
            key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        ),
    )


@pytest.mark.live
@pytest.mark.enable_socket
def test_org_owned_onboarding_proof_qualification_and_revocation(tmp_path):
    admin, runtime, tenant, other, _, _ = prepare_database()
    subject = str(uuid4())
    with psycopg.connect(admin) as c:
        org = c.execute(
            "SELECT id FROM iam.organizations WHERE tenant_id=%s", (tenant,)
        ).fetchone()[0]
        c.execute(
            "INSERT INTO iam.memberships(id,tenant_id,subject,role) VALUES(%s,%s,%s,'org_owner')",
            (uuid4(), tenant, subject),
        )
    principal = Principal(subject, "test", frozenset().union(*ROLE_SCOPES.values()))
    db = create_runtime_engine(runtime.replace("postgresql://", "postgresql+psycopg://", 1))
    private, secret, public = keys()
    _, coordinator_secret, coordinator_public = keys()
    coordinator = {
        "id": str(uuid4()),
        "key_id": "test-coordinator",
        "public_key": coordinator_public,
    }
    config = {
        "name": "Test institution provider",
        "endpoint": "https://operator.example/a2a",
        "private_key": secret,
        "public_key": public,
        "key_id": "test-service",
    }
    raw = card(config).model_dump(mode="json", by_alias=True, exclude_none=True)
    draft = AgentDraft(
        name=config["name"],
        endpoint=config["endpoint"],
        public_key=public,
        key_id=config["key_id"],
        card_digest=digest(raw),
        operator_mode="team_controlled",
        discoverable=True,
    )

    def call(fn, t=tenant):
        with TenantUnitOfWork(db, t) as s:
            return fn(Onboarding(s, t, org, principal))

    try:
        created = call(lambda a: a.create(draft, "draft-1"))
        identifier = created["id"]
        assert call(lambda a: a.create(draft, "draft-1"))["id"] == identifier
        with pytest.raises(IdempotencyConflict):
            call(lambda a: a.create(draft.model_copy(update={"name": "other"}), "draft-1"))
        with pytest.raises(LookupError):
            call(lambda a: a.row(identifier), other)
        with pytest.raises(ValueError):
            call(lambda a: a.transition(identifier, 1, "activate"))
        challenge = call(lambda a: a.challenge(identifier, 1, raw, coordinator))
        proof = AgentProof(
            expected_version=2,
            challenge_id=challenge["payload"]["challenge_id"],
            signature=b64(private.sign(canonical(challenge["payload"]))),
        )
        with psycopg.connect(admin) as connection:
            connection.execute(
                "UPDATE federation.registration_challenges SET expires_at=now()-interval '1 second' WHERE id=%s",
                (proof.challenge_id,),
            )
        with pytest.raises(ValueError):
            call(lambda a: a.prove(identifier, proof))
        with psycopg.connect(admin) as connection:
            connection.execute(
                "UPDATE federation.registration_challenges SET expires_at=%s WHERE id=%s",
                (challenge["payload"]["expires_at"], proof.challenge_id),
            )
        tampered = proof.model_copy(
            update={
                "signature": b64(Ed25519PrivateKey.generate().sign(canonical(challenge["payload"])))
            }
        )
        with pytest.raises(InvalidSignature):
            call(lambda a: a.prove(identifier, tampered))
        verified = call(lambda a: a.prove(identifier, proof))
        assert verified["state"] == "verified"
        with pytest.raises(StaleVersion):
            call(lambda a: a.prove(identifier, proof))
        with pytest.raises(ValueError):
            call(lambda a: a.prove(identifier, proof.model_copy(update={"expected_version": 3})))
        config.update(coordinator=coordinator, agent_id=str(identifier))
        row = call(lambda a: a.row(identifier))

        async def run():
            report = await qualify_async(
                row,
                coordinator,
                coordinator_secret,
                "qualification-1",
                httpx.ASGITransport(app=create_provider(config, tmp_path)),
            )
            # Recreate the provider app/task store: persisted operation artifacts must survive restart.
            again = await qualify_async(
                row,
                coordinator,
                coordinator_secret,
                "qualification-1",
                httpx.ASGITransport(app=create_provider(config, tmp_path)),
            )
            assert [c.get("artifact_digest") for c in report["checks"]] == [
                c.get("artifact_digest") for c in again["checks"]
            ]
            return report

        report = asyncio.run(run())
        assert report["status"] == "PASS"
        qualified = call(lambda a: a.qualify(identifier, 3, row["binding_version"], report))
        assert qualified["state"] == "qualified"
        active = call(lambda a: a.transition(identifier, 4, "activate"))
        assert active["service_eligible"]
        with db.connect() as connection:
            assert (
                len(
                    connection.execute(
                        text("SELECT * FROM iam.discover_institutional_agents()")
                    ).all()
                )
                == 1
            )
        assert not active["research_dispatch_enabled"]
        suspended = call(lambda a: a.transition(identifier, 5, "suspend"))
        assert not suspended["service_eligible"]
        revoked = call(lambda a: a.transition(identifier, 6, "revoke"))
        assert revoked["state"] == "revoked"
        with pytest.raises(ValueError):
            call(lambda a: a.revise(identifier, 7, draft))
        assert call(lambda a: a.history(identifier))[-1]["event_type"] == "revoke"
        with db.connect() as connection:
            assert not connection.execute(
                text("SELECT * FROM iam.discover_institutional_agents()")
            ).all()
    finally:
        db.dispose()


@pytest.mark.live
@pytest.mark.enable_socket
def test_http_qualification_job_is_durable_and_org_admin_only(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from network_setup import configuration

    from orca import onboarding_worker
    from orca.api.app import create_app
    from orca.api.dependencies import current_principal
    from orca.api.routers import agents as routes
    from orca.config import Settings
    from orca.infrastructure.federation.coordinator import load_coordinator

    admin, runtime, tenant, other, _, _ = prepare_database()
    subject = str(uuid4())
    with psycopg.connect(admin) as c:
        org = c.execute(
            "SELECT id FROM iam.organizations WHERE tenant_id=%s", (tenant,)
        ).fetchone()[0]
        other_org = c.execute(
            "SELECT id FROM iam.organizations WHERE tenant_id=%s", (other,)
        ).fetchone()[0]
        c.execute(
            "INSERT INTO iam.memberships(id,tenant_id,subject,role) VALUES(%s,%s,%s,'org_owner')",
            (uuid4(), tenant, subject),
        )
    principal = Principal(subject, "test", frozenset().union(*ROLE_SCOPES.values()))
    network, _, _ = configuration(tmp_path)
    c = network.configuration.coordinator
    coordinator = {"id": str(c.id), "key_id": c.key_id, "public_key": c.public_key}
    private, secret, public = keys()
    provider = {
        "name": "HTTP provider",
        "endpoint": "https://operator.example/a2a",
        "private_key": secret,
        "public_key": public,
        "key_id": "http-service",
    }
    raw = card(provider).model_dump(mode="json", by_alias=True, exclude_none=True)
    extra_endpoint = "https://secondary.example/a2a"
    extra_capability = "sst_review"
    extra_card = {
        **raw,
        "url": extra_endpoint,
        "skills": [{**raw["skills"][0], "id": extra_capability}],
    }
    body = {
        "name": provider["name"],
        "endpoint": provider["endpoint"],
        "public_key": public,
        "key_id": provider["key_id"],
        "card_digest": digest(raw),
        "operator_mode": "team_controlled",
        "additional_endpoints": [
            {
                "endpoint": extra_endpoint,
                "card_digest": digest(extra_card),
                "capabilities": [extra_capability],
            }
        ],
    }
    settings = Settings(
        monitoring_token="0123456789abcdefghijklmnopqrstuvwxyz",
        database_url=runtime.replace("postgresql://", "postgresql+psycopg://", 1),
        agent_onboarding_enabled=True,
        federation_coordinator_id=c.id,
        federation_coordinator_key_id=c.key_id,
        federation_coordinator_public_key=c.public_key,
        federation_coordinator_private_key=network.private_key,
    )
    api_settings = settings.model_copy(update={"federation_coordinator_private_key": None})
    app = create_app(api_settings)
    assert not settings.prototype_enabled and not settings.network_enabled
    assert not hasattr(app.state, "network_runtime")
    assert not hasattr(app.state.onboarding_coordinator, "private_key")
    federation_coordinator = load_coordinator(settings)
    app.dependency_overrides[current_principal] = lambda: principal
    monkeypatch.setattr(routes, "fetch_card", lambda endpoint: raw)
    with TestClient(app) as client:
        base = f"/v1/organizations/{org}/agents"
        response = client.post(base, json=body, headers={"Idempotency-Key": "http-create"})
        assert response.status_code == 201, response.text
        identifier = response.json()["id"]
        path = base + "/" + identifier
        checks_path = f"/v1/network/agents/{identifier}/endpoint-checks"
        checks = f"{checks_path}?organization_id={org}"
        directory = f"/v1/network/agents?organization_id={org}"
        assert (
            client.get(directory + f"&capability={extra_capability}").json()["items"][0]["id"]
            == identifier
        )
        requests = []

        def fetch_registered(endpoint):
            requests.append(endpoint)
            assert endpoint == extra_endpoint
            return extra_card

        monkeypatch.setattr(onboarding_worker, "fetch_card", fetch_registered)
        assert client.post(checks, json={"expected_version": 2}).status_code == 412
        assert (
            client.post(
                checks, json={"expected_version": 1, "endpoint": "https://unregistered.example/a2a"}
            ).status_code
            == 404
        )
        check_request = {"expected_version": 1, "endpoint": extra_endpoint}
        queued = client.post(checks, json=check_request)
        assert queued.status_code == 202, queued.text
        check_id = queued.json()["id"]
        assert client.post(checks, json=check_request).json()["id"] == check_id
        assert client.post(checks, json={"expected_version": 1}).status_code == 429
        assert requests == []
        poll = f"{checks_path}/{check_id}?organization_id={org}"
        assert client.get(poll).json()["state"] == "queued"
        assert onboarding_worker.execute(
            app.state.runtime_engine, federation_coordinator, tenant, UUID(check_id)
        )
        observed = client.get(poll).json()
        assert observed["state"] == "succeeded"
        assert observed["report"]["status"] == "matches_registration"
        assert observed["endpoint"] == observed["report"]["endpoint"] == extra_endpoint
        assert observed["report"]["binding_version"] == response.json()["binding_version"]
        assert client.get(path + f"/qualifications/{check_id}").status_code == 404
        cooldown = client.post(checks, json={"expected_version": 1})
        assert cooldown.status_code == 429 and cooldown.headers["Retry-After"] == "60"
        assert client.get(path).json()["state"] == "draft"
        events = client.get(path + "/events").json()
        assert [
            item["data"]["status"] for item in events if item["event_type"] == "endpoint_checked"
        ] == ["matches_registration"]
        assert client.get(f"/v1/organizations/{other_org}/agents").status_code in (403, 404)
        challenge = client.post(
            path + "/verification-challenges", json={"expected_version": 1}
        ).json()
        assert (
            client.get(path + "/verification-challenges").json()["payload"] == challenge["payload"]
        )
        signature = b64(private.sign(canonical(challenge["payload"])))
        assert (
            client.post(
                path + "/verification-proofs",
                json={
                    "expected_version": 2,
                    "challenge_id": challenge["payload"]["challenge_id"],
                    "signature": signature,
                },
            ).status_code
            == 200
        )
        provider.update(coordinator=coordinator, agent_id=identifier)

        def qualify(agent, coordinator, secret, key):
            return asyncio.run(
                qualify_async(
                    agent,
                    coordinator,
                    secret,
                    key,
                    httpx.ASGITransport(app=create_provider(provider, tmp_path / "provider")),
                )
            )

        monkeypatch.setattr(onboarding_worker, "qualify", qualify)
        job = client.post(
            path + "/qualifications",
            json={"expected_version": 3},
            headers={"Idempotency-Key": "qualify-1"},
        )
        assert job.status_code == 202, job.text
        again = client.post(
            path + "/qualifications",
            json={"expected_version": 3},
            headers={"Idempotency-Key": "qualify-1"},
        )
        assert again.json()["id"] == job.json()["id"]
        assert onboarding_worker.execute(
            app.state.runtime_engine, federation_coordinator, tenant, UUID(job.json()["id"])
        )
        assert not onboarding_worker.execute(
            app.state.runtime_engine, federation_coordinator, tenant, UUID(job.json()["id"])
        )
        assert (
            client.get(path + "/qualifications/" + job.json()["id"]).json()["state"] == "succeeded"
        )
        assert (
            client.post(path + "/activate", json={"expected_version": 4}).json()["state"]
            == "active"
        )
        network_path = f"/v1/network/agents/{identifier}"
        capabilities = client.get(f"{network_path}/capabilities?organization_id={org}").json()[
            "items"
        ]
        assert {item["capability_id"]: item["qualified_for_service"] for item in capabilities} == {
            "temperature_summary_v1": True,
            extra_capability: False,
        }
        endpoints = client.get(f"{network_path}/endpoints?organization_id={org}").json()["items"]
        assert [(item["uri"], item["service_eligible"]) for item in endpoints] == [
            (provider["endpoint"], True),
            (extra_endpoint, False),
        ]
        # Editing an endpoint/key revision invalidates service eligibility and old qualification.
        with psycopg.connect(admin) as connection:
            connection.execute(
                "UPDATE federation.registration_jobs SET created_at=now()-interval '61 seconds' WHERE id=%s",
                (check_id,),
            )
        stale = client.post(checks, json={"expected_version": 5})
        assert stale.status_code == 202, stale.text
        stale_id = stale.json()["id"]

        def revise_during_fetch(endpoint):
            requests.append(endpoint)
            changed = client.patch(
                path,
                json={
                    "expected_version": 5,
                    "registration": {
                        **body,
                        "name": "Revised",
                        "additional_endpoints": [],
                        "additional_capabilities": ["ocean_metadata"],
                    },
                },
            )
            assert changed.status_code == 200 and changed.json()["state"] == "draft"
            assert not changed.json()["service_eligible"]
            return {**raw, "name": "Changed Card"}

        monkeypatch.setattr(onboarding_worker, "fetch_card", revise_during_fetch)
        assert onboarding_worker.execute(
            app.state.runtime_engine, federation_coordinator, tenant, UUID(stale_id)
        )
        discarded = client.get(f"{checks_path}/{stale_id}?organization_id={org}").json()
        assert discarded["state"] == "failed" and discarded["report"] is None
        assert client.get(directory + f"&capability={extra_capability}").json()["items"] == []
        assert (
            client.get(directory + "&capability=ocean_metadata").json()["items"][0]["id"]
            == identifier
        )
        assert [
            item["data"]["status"]
            for item in client.get(path + "/events").json()
            if item["event_type"] == "endpoint_checked"
        ] == ["matches_registration"]
        assert client.post(path + "/activate", json={"expected_version": 6}).status_code == 409
        # A reader may inspect but cannot register or mutate organizational services.
        with psycopg.connect(admin) as connection:
            connection.execute(
                "UPDATE iam.memberships SET role='viewer' WHERE tenant_id=%s AND subject=%s",
                (tenant, subject),
            )
        assert client.get(base).status_code == 200
        assert client.post(checks, json={"expected_version": 6}).status_code == 403
        assert len(requests) == 2
        assert (
            client.post(base, json=body, headers={"Idempotency-Key": "reader-create"}).status_code
            == 403
        )
