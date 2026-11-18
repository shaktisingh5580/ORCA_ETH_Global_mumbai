"""Purpose: verify P09 remote intents are tied to durable approved runs.
Behavior: reject absent authority, cap concurrent allocations and preserve idempotent state.
Refs: main_implementation plan.md P09.3; api design.md §§2.2,17.1; Phase: P09.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from live_database import prepare_database
from psycopg.types.json import Jsonb

from orca.application.investigations import Investigations
from orca.application.remote_dispatches import RemoteDispatches
from orca.contracts.investigations import ApprovePlan, CreateInvestigation
from orca.contracts.research_plans import PlanNode, TypedPlan
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.jobs.postgres_queue import EnqueuedRun, PostgresQueue


@pytest.mark.live
def test_remote_dispatch_requires_current_authority_and_reserved_budget() -> None:
    admin, runtime, tenant, _, project, _ = prepare_database()
    subject = str(uuid4())
    agent_id, binding, snapshot, version, product = [uuid4() for _ in range(5)]
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
        with psycopg.connect(admin) as connection:
            connection.execute(
                "UPDATE federation.research_authorizations SET revoked_at=clock_timestamp() "
                "WHERE tenant_id=%s AND agent_id=%s",
                (tenant, agent_id),
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
