"""Purpose: build approved P05 queue commands for disposable PostgreSQL tests.
Behavior: create a tenant-scoped investigation and exact approved plan before queue assertions.
Refs: main_implementation plan.md P05.1-P05.2; api design.md §§5.1, 18.1; Phase: P05 test support.
"""

from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
from live_database import prepare_database

from orca.application.investigations import Investigations
from orca.contracts.investigations import ApprovePlan, CreateInvestigation
from orca.contracts.research_plans import PlanNode, TypedPlan
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.jobs.postgres_queue import EnqueuedRun


def queue_command(
    investigation_id: UUID, plan_id: UUID, digest: str, key: str, amount: str = "2.00"
) -> EnqueuedRun:
    """Build a distinct idempotent command for an already-approved test plan."""
    return EnqueuedRun(
        investigation_id=investigation_id,
        plan_id=plan_id,
        approved_digest=digest,
        plan_budget=Decimal("5.00"),
        execution_budget=Decimal(amount),
        request_digest="sha256:" + ("a" if key.endswith("a") else "b") * 64,
        idempotency_key=key,
        actor="queue-test",
        payload={"kind": "investigation_run", "plan_id": str(plan_id)},
    )


def approved_queue() -> tuple[object, UUID, UUID, UUID, EnqueuedRun]:
    """Seed one exact approval and return its authenticated queue command."""
    admin, runtime, tenant, _, project, _ = prepare_database()
    subject = str(uuid4())
    with psycopg.connect(admin) as connection:
        connection.execute(
            "INSERT INTO iam.memberships(id,tenant_id,subject,role) VALUES(%s,%s,%s,'org_owner')",
            (uuid4(), tenant, subject),
        )
    db = create_runtime_engine(runtime)
    principal = Principal(subject, "test", frozenset().union(*ROLE_SCOPES.values()))
    with TenantUnitOfWork(db, tenant) as session:
        service = Investigations(session, tenant, project, principal)
        investigation = service.create(
            CreateInvestigation(
                project_id=project,
                question="Does the queued plan preserve exactly one approved recipe?",
                geo_scope={
                    "type": "Point",
                    "geometry": {"type": "Point", "coordinates": [68.5, 20.5]},
                },
                time_window={"start": "2025-01-01T00:00:00Z", "end": "2025-02-01T00:00:00Z"},
                risk_tier="research",
            ),
            "queue-draft",
        )
        candidate = service.create_plan(
            investigation.id,
            TypedPlan(
                revision=1,
                method_hint="sst.seasonal_anomaly",
                data_constraints={"source_ready": False},
                budget={"max_amount": "5.00"},
                nodes=[
                    PlanNode(node_id="source-metadata", operation="source_metadata", depends_on=[])
                ],
                assumptions=[],
                source_ids=[],
            ),
            "queue-plan",
            maximum_budget=Decimal("5"),
        )
        service.approve_plan(
            investigation.id,
            candidate.id,
            ApprovePlan(digest=candidate.plan_digest, accepted_assumptions=[]),
            candidate.etag,
            "queue-approval",
        )
    return (
        db,
        tenant,
        project,
        investigation.id,
        queue_command(investigation.id, candidate.id, candidate.plan_digest, "queue-a"),
    )
