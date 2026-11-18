"""Purpose: verify P09 remote intents are tied to durable approved runs.
Behavior: reject absent authority, cap concurrent allocations and preserve idempotent state.
Refs: main_implementation plan.md P09.3; api design.md §§2.2,17.1; Phase: P09.
"""

import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from live_database import prepare_database
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from orca.api.app import create_app
from orca.api.dependencies import current_principal
from orca.application.investigations import Investigations
from orca.application.remote_dispatch_worker import LeasedDispatch, RemoteDispatchWorker
from orca.application.remote_dispatches import RemoteDispatches
from orca.config import Settings
from orca.contracts.investigations import ApprovePlan, CreateInvestigation
from orca.contracts.research_plans import PlanNode, TypedPlan
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.federation.coordinator import FederationCoordinator
from orca.infrastructure.federation.signing import b64, canonical, digest
from orca.infrastructure.jobs.postgres_queue import EnqueuedRun, PostgresQueue


@pytest.mark.live
@pytest.mark.enable_socket
def test_remote_dispatch_requires_current_authority_and_reserved_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin, runtime, tenant, _, project, _ = prepare_database()
    subject = str(uuid4())
    agent_id, binding, snapshot, version, product = [uuid4() for _ in range(5)]
    provider_key, coordinator_key = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    provider_public = b64(
        provider_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    )
    coordinator = FederationCoordinator(
        uuid4(),
        "coordinator-1",
        b64(
            coordinator_key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        ),
        b64(
            coordinator_key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        ),
    )
    with psycopg.connect(admin) as connection:
        organization = connection.execute(
            "SELECT id FROM iam.organizations WHERE tenant_id=%s", (tenant,)
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO iam.memberships(id,tenant_id,subject,role) VALUES(%s,%s,%s,'org_owner')",
            (uuid4(), tenant, subject),
        )
        connection.execute(
            "INSERT INTO research.catalog_products(id,tenant_id,provider_id,source_product_id,kind) "
            "VALUES(%s,%s,'test','SST','observation')",
            (product, tenant),
        )
        connection.execute(
            "INSERT INTO research.catalog_versions(id,tenant_id,product_id,source_version,metadata_digest,manifest) "
            "VALUES(%s,%s,%s,'v1',%s,%s)",
            (version, tenant, product, "sha256:" + "a" * 64, Jsonb({})),
        )
        connection.execute(
            "INSERT INTO research.dataset_snapshots"
            "(id,tenant_id,version_id,raw_content_digest,byte_count,manifest) "
            "VALUES(%s,%s,%s,%s,12,%s)",
            (snapshot, tenant, version, "sha256:" + "b" * 64, Jsonb({})),
        )
        connection.execute(
            "INSERT INTO research.snapshot_project_grants(tenant_id,project_id,snapshot_id) "
            "VALUES(%s,%s,%s)",
            (tenant, project, snapshot),
        )
        connection.execute(
            "INSERT INTO federation.registrations"
            "(id,tenant_id,organization_id,created_by,idempotency_key,request_digest,binding_version,state) "
            "VALUES(%s,%s,%s,%s,'remote-agent',%s,%s,'active')",
            (agent_id, tenant, organization, subject, "sha256:" + "c" * 64, binding),
        )
        connection.execute(
            "INSERT INTO federation.registration_versions"
            "(id,tenant_id,organization_id,agent_id,registration) VALUES(%s,%s,%s,%s,%s)",
            (
                binding,
                tenant,
                organization,
                agent_id,
                Jsonb(
                    {
                        "capability": "temperature_summary_v1",
                        "endpoint": "https://provider.example/a2a",
                        "key_id": "provider-1",
                        "public_key": provider_public,
                    }
                ),
            ),
        )
        connection.execute(
            "INSERT INTO federation.agent_qualifications"
            "(id,tenant_id,organization_id,agent_id,binding_version,report,expires_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                uuid4(),
                tenant,
                organization,
                agent_id,
                binding,
                Jsonb({"status": "PASS"}),
                datetime.now(timezone.utc) + timedelta(hours=1),
            ),
        )
    db = create_runtime_engine(runtime)
    principal = Principal(subject, "test", frozenset().union(*ROLE_SCOPES.values()))
    try:
        with TenantUnitOfWork(db, tenant) as session:
            service = Investigations(session, tenant, project, principal)
            investigation = service.create(
                CreateInvestigation(
                    project_id=project,
                    question="Can a scoped provider reproduce this SST result?",
                    geo_scope={
                        "type": "Point",
                        "geometry": {"type": "Point", "coordinates": [68.5, 20.5]},
                    },
                    time_window={"start": "2025-01-01T00:00:00Z", "end": "2025-02-01T00:00:00Z"},
                    risk_tier="research",
                ),
                "remote-investigation",
            )
            plan = service.create_plan(
                investigation.id,
                TypedPlan(
                    revision=1,
                    method_hint="sst.seasonal_anomaly",
                    data_constraints={"source_ready": False},
                    budget={"max_amount": "5.00"},
                    nodes=[
                        PlanNode(
                            node_id="source",
                            operation="source_metadata",
                            depends_on=[],
                            source_ids=[snapshot],
                        )
                    ],
                    assumptions=[],
                    source_ids=[snapshot],
                ),
                "remote-plan",
                maximum_budget=Decimal("5.00"),
            )
            service.approve_plan(
                investigation.id,
                plan.id,
                ApprovePlan(digest=plan.plan_digest, accepted_assumptions=[]),
                plan.etag,
                "remote-approval",
            )
            run = PostgresQueue(session, tenant, project).enqueue_run(
                EnqueuedRun(
                    investigation.id,
                    plan.id,
                    plan.plan_digest,
                    Decimal("5.00"),
                    Decimal("2.00"),
                    "sha256:" + "d" * 64,
                    "remote-run",
                    subject,
                    {"kind": "investigation_run", "node_count": 1},
                )
            )

        with pytest.raises(PermissionError, match="approval is missing"):
            with TenantUnitOfWork(db, tenant) as session:
                RemoteDispatches(session, tenant, project, principal).enqueue(
                    run.run_id,
                    agent_id,
                    "temperature_summary_v1",
                    "sst.seasonal_anomaly",
                    Decimal("1.25"),
                    "remote-1",
                )
        with psycopg.connect(admin) as connection:
            assert not connection.execute(
                "SELECT has_table_privilege('orca_runtime','federation.research_authorizations','INSERT')"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO federation.research_authorizations"
                "(id,tenant_id,organization_id,project_id,agent_id,binding_version,capability_id,method_id,expires_at,approved_by) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    uuid4(),
                    tenant,
                    organization,
                    project,
                    agent_id,
                    binding,
                    "temperature_summary_v1",
                    "sst.seasonal_anomaly",
                    datetime.now(timezone.utc) + timedelta(minutes=30),
                    "test-reviewer",
                ),
            )
        with TenantUnitOfWork(db, tenant) as session:
            dispatch = RemoteDispatches(session, tenant, project, principal).enqueue(
                run.run_id,
                agent_id,
                "temperature_summary_v1",
                "sst.seasonal_anomaly",
                Decimal("1.25"),
                "remote-1",
            )
            assert dispatch.state == "queued"
        with TenantUnitOfWork(db, tenant) as session:
            service = RemoteDispatches(session, tenant, project, principal)
            assert (
                service.enqueue(
                    run.run_id,
                    agent_id,
                    "temperature_summary_v1",
                    "sst.seasonal_anomaly",
                    Decimal("1.25"),
                    "remote-1",
                ).id
                == dispatch.id
            )
            with pytest.raises(ValueError, match="reserved run budget"):
                service.enqueue(
                    run.run_id,
                    agent_id,
                    "temperature_summary_v1",
                    "sst.seasonal_anomaly",
                    Decimal("1.00"),
                    "remote-2",
                )

        async def respond(signed, agent, scope):
            payload = {
                "schema_version": "orca.scientific_artifact/1.0",
                "grant_digest": signed.grant_digest,
                "task_id": str(signed.grant.task_id),
                "run_id": str(signed.grant.run_id),
                "approved_plan_digest": signed.grant.approved_plan_digest,
                "method_id": signed.grant.method_id,
                "dataset_ids": [str(item) for item in signed.grant.dataset_ids],
                "a2a_task_id": "remote-task-1",
                "a2a_context_id": "remote-context-1",
                "computed": {"mean_celsius": 28.5},
            }
            return {
                "a2a_task_id": "remote-task-1",
                "a2a_context_id": "remote-context-1",
                "artifact_digest": digest(payload),
                "artifact": payload,
                "signed_artifact": {
                    "payload": payload,
                    "key_id": "provider-1",
                    "signature": b64(provider_key.sign(canonical(payload))),
                },
            }

        worker = RemoteDispatchWorker(
            tenant, project, lambda: TenantUnitOfWork(db, tenant), coordinator, respond
        )
        assert worker.run_once() == "completed"
        with psycopg.connect(admin) as connection:
            saved = connection.execute(
                "SELECT state,generation,grant_digest,signed_grant,verified_response,result_digest "
                "FROM federation.research_dispatches WHERE id=%s",
                (dispatch.id,),
            ).fetchone()
            assert saved[0] == "completed" and saved[1] == 1
            assert saved[2] == saved[3]["grant_digest"]
            assert saved[4]["artifact_digest"] == saved[5]
        with TenantUnitOfWork(db, tenant) as session:
            assert not worker._finish(
                session,
                LeasedDispatch(dispatch.id, run.run_id, uuid4(), 1, None),
                "failed",
                "STALE_WORKER",
            )
        app = create_app(
            Settings(monitoring_token=SecretStr(secrets.token_urlsafe(48)), environment="test")
        )
        app.state.runtime_engine = db
        app.dependency_overrides[current_principal] = lambda: principal
        base = f"/v1/research/investigations/{investigation.id}/runs/{run.run_id}/remote-dispatches"
        with TestClient(app) as client:
            created = client.post(
                base,
                headers={"Idempotency-Key": "remote-cancel"},
                json={
                    "agent_id": str(agent_id),
                    "capability_id": "temperature_summary_v1",
                    "method_id": "sst.seasonal_anomaly",
                    "budget_usd": "0.25",
                },
            )
            assert created.status_code == 202, created.text
            dispatch_id = created.json()["id"]
            path = f"{base}/{dispatch_id}"
            assert client.get(path).json()["state"] == "queued"
            foreign = path.replace(str(run.run_id), str(uuid4()), 1)
            assert client.get(foreign).status_code == 404
            stopped = client.post(f"{path}/cancel")
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["error_code"] == "LOCAL_CANCELLED_BEFORE_SEND"
            assert client.post(f"{path}/cancel").json()["generation"] == 1
        with psycopg.connect(admin) as connection:
            assert connection.execute(
                "SELECT generation,lease_token,verified_response FROM federation.research_dispatches WHERE id=%s",
                (dispatch_id,),
            ).fetchone() == (1, None, None)

        async def cancel_during_call(signed, agent, scope):
            with TenantUnitOfWork(db, tenant) as session:
                row = cancelling_worker._row(session, signed.grant.task_id)
                assert row is not None
                lease = LeasedDispatch(
                    row["id"], row["run_id"], row["lease_token"], row["generation"], signed
                )
                assert RemoteDispatches(session, tenant, project, principal).cancel(
                    run.run_id, signed.grant.task_id
                )
            cancelling_worker._checkpoint(lease, "remote-task-1", "remote-context-1")
            return await respond(signed, agent, scope)

        with TenantUnitOfWork(db, tenant) as session:
            service = RemoteDispatches(session, tenant, project, principal)
            in_flight = service.enqueue(
                run.run_id,
                agent_id,
                "temperature_summary_v1",
                "sst.seasonal_anomaly",
                Decimal("0.25"),
                "remote-in-flight",
            )
        cancelling_worker = RemoteDispatchWorker(
            tenant, project, lambda: TenantUnitOfWork(db, tenant), coordinator, cancel_during_call
        )
        assert cancelling_worker.run_once() == "lease_lost"
        cancellation_requests = []

        async def confirm_cancellation(signed, registration, identity, remote_ref):
            assert signed.grant.task_id == in_flight.id
            assert identity == coordinator
            assert registration["endpoint"] == "https://provider.example/a2a"
            cancellation_requests.append(remote_ref)
            return True

        monkeypatch.setattr(
            "orca.application.remote_dispatch_worker.cancel_research_task", confirm_cancellation
        )
        assert cancelling_worker.run_once() == "remote_cancel_attempted"
        assert cancelling_worker.run_once() == "idle"
        assert cancellation_requests == [("remote-task-1", "remote-context-1")]
        with TenantUnitOfWork(db, tenant) as session:
            service = RemoteDispatches(session, tenant, project, principal)
            view = service.get(run.run_id, in_flight.id)
            assert view["state"] == "cancelled" and view["verified_response"] is None
            assert view["remote_cancellation_confirmed"] is True
            with pytest.raises(ValueError, match="reserved run budget"):
                service.enqueue(
                    run.run_id,
                    agent_id,
                    "temperature_summary_v1",
                    "sst.seasonal_anomaly",
                    Decimal("0.51"),
                    "remote-uncertain-budget",
                )

        with TenantUnitOfWork(db, tenant) as session:
            revoked_dispatch = RemoteDispatches(session, tenant, project, principal).enqueue(
                run.run_id,
                agent_id,
                "temperature_summary_v1",
                "sst.seasonal_anomaly",
                Decimal("0.50"),
                "remote-revoke",
            )

        async def revoke_during_call(signed, agent, scope):
            with psycopg.connect(admin) as connection:
                connection.execute(
                    "UPDATE federation.research_authorizations SET revoked_at=clock_timestamp() "
                    "WHERE tenant_id=%s AND agent_id=%s",
                    (tenant, agent_id),
                )
            return await respond(signed, agent, scope)

        revoked_worker = RemoteDispatchWorker(
            tenant, project, lambda: TenantUnitOfWork(db, tenant), coordinator, revoke_during_call
        )
        assert revoked_worker.run_once() == "rejected"
        with psycopg.connect(admin) as connection:
            assert connection.execute(
                "SELECT state,verified_response FROM federation.research_dispatches WHERE id=%s",
                (revoked_dispatch.id,),
            ).fetchone() == ("failed", None)
            connection.execute(
                "UPDATE federation.research_dispatches SET state='leased',lease_token=%s,"
                "lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
                (uuid4(), revoked_dispatch.id),
            )
        assert revoked_worker.run_once() == "reconciliation_required"
        with psycopg.connect(admin) as connection:
            assert (
                connection.execute(
                    "SELECT state FROM federation.research_dispatches WHERE id=%s",
                    (revoked_dispatch.id,),
                ).fetchone()[0]
                == "reconciliation_required"
            )
        with pytest.raises(PermissionError, match="approval is missing"):
            with TenantUnitOfWork(db, tenant) as session:
                RemoteDispatches(session, tenant, project, principal).enqueue(
                    run.run_id,
                    agent_id,
                    "temperature_summary_v1",
                    "sst.seasonal_anomaly",
                    Decimal("0.50"),
                    "remote-3",
                )
    finally:
        db.dispose()
