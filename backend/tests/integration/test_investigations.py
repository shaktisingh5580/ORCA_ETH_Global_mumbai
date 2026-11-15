"""Purpose: verify durable P05 investigation revisions and scoped authorization.
Behavior: exercise idempotency, ETags, append-only plans and tenant concealment in Postgres.
Refs: main_implementation plan.md P05.1; api design.md §3.6; Phase: P05.
"""

from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from live_database import prepare_database
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from orca.application.investigations import Investigations
from orca.application.projects import IdempotencyConflict, PreconditionRequired, StaleVersion
from orca.contracts.investigations import CreateInvestigation, PatchInvestigation
from orca.contracts.research_plans import PlanNode, TypedPlan
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork


def investigation(project_id: UUID) -> CreateInvestigation:
    return CreateInvestigation(
        project_id=project_id,
        question="What is the seasonal SST pattern?",
        geo_scope={
            "type": "Point",
            "geometry": {"type": "Point", "coordinates": [68.5, 20.5]},
        },
        time_window={"start": "2025-01-01T00:00:00Z", "end": "2025-02-01T00:00:00Z"},
        risk_tier="research",
    )


def typed_plan(revision: int, question: str = "Pinned product candidate") -> TypedPlan:
    return TypedPlan(
        revision=revision,
        method_hint="sst.seasonal_anomaly",
        data_constraints={"question": question},
        budget={"max_amount": "2.00"},
        nodes=[PlanNode(node_id="source", operation="source_metadata", depends_on=[])],
        assumptions=[],
        source_ids=[],
    )


@pytest.mark.live
def test_investigation_idempotency_etag_plan_revisions_and_tenant_scope():
    admin, runtime, tenant, other_tenant, project, other_project = prepare_database()
    subject, other_subject = str(uuid4()), str(uuid4())
    with psycopg.connect(admin) as connection:
        connection.execute(
            "INSERT INTO iam.memberships(id,tenant_id,subject,role) VALUES(%s,%s,%s,'org_owner'),(%s,%s,%s,'org_owner')",
            (uuid4(), tenant, subject, uuid4(), other_tenant, other_subject),
        )
    db = create_runtime_engine(runtime)
    principal = Principal(subject, "test", frozenset().union(*ROLE_SCOPES.values()))
    other_principal = Principal(other_subject, "test", frozenset().union(*ROLE_SCOPES.values()))
    try:
        with TenantUnitOfWork(db, tenant) as session:
            service = Investigations(session, tenant, project, principal)
            draft = service.create(investigation(project), "investigation-key")
            assert service.create(investigation(project), "investigation-key").id == draft.id
            assert draft.state == "DRAFT" and draft.etag == '"1"'
        with pytest.raises(IdempotencyConflict), TenantUnitOfWork(db, tenant) as session:
            Investigations(session, tenant, project, principal).create(
                investigation(project).model_copy(
                    update={"question": "Changed question, different body"}
                ),
                "investigation-key",
            )
        with pytest.raises(PreconditionRequired), TenantUnitOfWork(db, tenant) as session:
            Investigations(session, tenant, project, principal).patch(
                draft.id, PatchInvestigation(description="notes"), None
            )
        with pytest.raises(StaleVersion), TenantUnitOfWork(db, tenant) as session:
            Investigations(session, tenant, project, principal).patch(
                draft.id, PatchInvestigation(description="notes"), '"0"'
            )
        with TenantUnitOfWork(db, tenant) as session:
            service = Investigations(session, tenant, project, principal)
            revised = service.patch(draft.id, PatchInvestigation(description="notes"), draft.etag)
            assert revised.version == 2
        with TenantUnitOfWork(db, tenant) as session:
            service = Investigations(session, tenant, project, principal)
            first = service.create_plan(
                draft.id, typed_plan(1), "plan-key-1", maximum_budget=Decimal("5")
            )
            assert (
                service.create_plan(
                    draft.id, typed_plan(1), "plan-key-1", maximum_budget=Decimal("5")
                ).id
                == first.id
            )
            second = service.create_plan(
                draft.id,
                typed_plan(2, "Second source candidate"),
                "plan-key-2",
                maximum_budget=Decimal("5"),
            )
            assert [plan.revision for plan in service.plans(draft.id)] == [1, 2]
            assert second.plan_digest != first.plan_digest
            assert service.get_plan(draft.id, first.id).id == first.id
        with (
            pytest.raises(SQLAlchemyError, match="immutable"),
            TenantUnitOfWork(db, tenant) as session,
        ):
            session.execute(
                text("UPDATE research.investigation_plans SET plan='{}' WHERE id=:id"),
                {"id": first.id},
            )
        with TenantUnitOfWork(db, other_tenant) as session:
            service = Investigations(session, other_tenant, other_project, other_principal)
            with pytest.raises(LookupError):
                service.get(draft.id)
    finally:
        db.dispose()
