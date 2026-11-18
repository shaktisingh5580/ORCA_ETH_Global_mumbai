"""Purpose: reserve a durable P09 remote task against one approved research run.
Behavior: atomically check current provider authority, source grants and remaining run budget.
Refs: main_implementation plan.md P09.3; api design.md §§2.2,17.1; Phase: P09.
"""

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.application.projects import IdempotencyConflict
from orca.application.workspace_access import authorize_project
from orca.contracts.remote_dispatch import RemoteDispatchEvent, RemoteDispatchHistory
from orca.contracts.research_plans import TypedPlan
from orca.domain.identity import Principal
from orca.infrastructure.federation.signing import digest


@dataclass(frozen=True, slots=True)
class DispatchReceipt:
    id: UUID
    run_id: UUID
    agent_id: UUID
    state: str


class RemoteDispatches:
    """Insert task intents only inside a tenant-scoped transaction."""

    def __init__(self, session: Session, tenant: UUID, project: UUID, principal: Principal) -> None:
        self.session, self.tenant, self.project, self.principal = (
            session,
            tenant,
            project,
            principal,
        )

    def get(self, run_id: UUID, dispatch_id: UUID) -> dict[str, object]:
        """Expose durable transport state without implying scientific acceptance."""
        authorize_project(self.session, self.tenant, self.project, self.principal, "project:read")
        row = (
            self.session.execute(
                text("""SELECT id,run_id,agent_id,binding_version,capability_id,method_id,
              plan_digest,dataset_ids,budget_usd::text AS budget_usd,state,generation,grant_digest,result_digest,
              verified_response,error_code,created_at,updated_at,
              remote_cancel_confirmed_at IS NOT NULL AS remote_cancellation_confirmed
              FROM federation.research_dispatches WHERE tenant_id=:tenant AND project_id=:project
                AND run_id=:run AND id=:id"""),
                {"tenant": self.tenant, "project": self.project, "run": run_id, "id": dispatch_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("Remote dispatch not found")
        return dict(row)

    def history(
        self, run_id: UUID, dispatch_id: UUID, after: int, limit: int
    ) -> RemoteDispatchHistory:
        self.get(run_id, dispatch_id)
        rows = (
            self.session.execute(
                text("""SELECT sequence,kind,run_id,agent_id,binding_version,generation,state,
              grant_digest,result_digest,remote_task_id,remote_context_id,error_code,
              remote_cancellation_confirmed,observed_at
              FROM federation.research_dispatch_events
              WHERE tenant_id=:tenant AND project_id=:project AND run_id=:run
                AND dispatch_id=:id AND sequence>:after ORDER BY sequence LIMIT :count"""),
                {
                    "tenant": self.tenant,
                    "project": self.project,
                    "run": run_id,
                    "id": dispatch_id,
                    "after": after,
                    "count": limit + 1,
                },
            )
            .mappings()
            .all()
        )
        items = [RemoteDispatchEvent.model_validate(dict(row)) for row in rows[:limit]]
        return RemoteDispatchHistory(
            items=items, next_after=items[-1].sequence if len(rows) > limit else None
        )

    def cancel(self, run_id: UUID, dispatch_id: UUID) -> bool:
        """Fence a local intent; a remote provider may still need separate cancellation."""
        authorize_project(self.session, self.tenant, self.project, self.principal, "research:run")
        state = self.session.execute(
            text("""SELECT state FROM federation.research_dispatches
              WHERE tenant_id=:tenant AND project_id=:project AND run_id=:run AND id=:id
              FOR UPDATE"""),
            {"tenant": self.tenant, "project": self.project, "run": run_id, "id": dispatch_id},
        ).scalar_one_or_none()
        if state is None:
            raise LookupError("Remote dispatch not found")
        if state == "cancelled":
            return False
        if state in {"completed", "failed"}:
            raise ValueError("Completed remote dispatch cannot be cancelled")
        self.session.execute(
            text("""UPDATE federation.research_dispatches
              SET state='cancelled',generation=generation+1,lease_token=NULL,lease_until=NULL,
                error_code=CASE WHEN signed_grant IS NULL THEN 'LOCAL_CANCELLED_BEFORE_SEND'
                  ELSE 'LOCAL_CANCELLED_REMOTE_UNCONFIRMED' END,updated_at=clock_timestamp()
              WHERE tenant_id=:tenant AND project_id=:project AND run_id=:run AND id=:id"""),
            {"tenant": self.tenant, "project": self.project, "run": run_id, "id": dispatch_id},
        )
        return True

    def enqueue(
        self,
        run_id: UUID,
        agent_id: UUID,
        capability_id: str,
        method_id: str,
        budget_usd: Decimal,
        idempotency_key: str,
    ) -> DispatchReceipt:
        authorize_project(self.session, self.tenant, self.project, self.principal, "research:run")
        if not 1 <= len(idempotency_key) <= 128 or not 1 <= len(capability_id) <= 100:
            raise ValueError("Invalid remote dispatch identifier")
        exponent = budget_usd.as_tuple().exponent
        if (
            not 1 <= len(method_id) <= 120
            or not budget_usd.is_finite()
            or budget_usd <= 0
            or budget_usd > 100
            or not isinstance(exponent, int)
            or exponent < -8
        ):
            raise ValueError("Invalid remote method or budget")
        params = {"tenant": self.tenant, "project": self.project, "run": run_id}
        request_digest = digest(
            {
                "run_id": str(run_id),
                "agent_id": str(agent_id),
                "capability_id": capability_id,
                "method_id": method_id,
                "budget_usd": format(budget_usd, "f"),
            }
        )
        existing = (
            self.session.execute(
                text("""SELECT id,agent_id,state,request_digest FROM federation.research_dispatches
              WHERE tenant_id=:tenant AND project_id=:project AND run_id=:run
                AND idempotency_key=:key"""),
                {**params, "key": idempotency_key},
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if existing["request_digest"] != request_digest:
                raise IdempotencyConflict()
            return DispatchReceipt(existing["id"], run_id, existing["agent_id"], existing["state"])

        run = (
            self.session.execute(
                text("""SELECT r.state,r.plan_digest,r.execution_budget,p.plan
              FROM research.compute_runs r JOIN research.investigation_plans p
                ON p.tenant_id=r.tenant_id AND p.project_id=r.project_id AND p.id=r.plan_id
              WHERE r.tenant_id=:tenant AND r.project_id=:project AND r.id=:run
              FOR UPDATE OF r"""),
                params,
            )
            .mappings()
            .one_or_none()
        )
        if run is None or run["state"] not in {"QUEUED", "RUNNING"}:
            raise ValueError("Only an active approved run can delegate research")
        plan = TypedPlan.model_validate(run["plan"])
        if plan.method_hint != method_id or not plan.source_ids:
            raise ValueError("Remote method and source snapshots must match the approved plan")
        for source_id in plan.source_ids:
            granted = self.session.execute(
                text("""SELECT 1 FROM research.snapshot_project_grants
                  WHERE tenant_id=:tenant AND project_id=:project AND snapshot_id=:source
                    AND (expires_at IS NULL OR expires_at>clock_timestamp())"""),
                {**params, "source": source_id},
            ).scalar_one_or_none()
            if granted is None:
                raise PermissionError("Approved source snapshot is no longer granted")

        agent = (
            self.session.execute(
                text("""SELECT r.organization_id,r.state,r.binding_version,v.registration,
              (SELECT max(q.expires_at) FROM federation.agent_qualifications q
                WHERE q.tenant_id=r.tenant_id AND q.agent_id=r.id
                  AND q.binding_version=r.binding_version AND q.report->>'status'='PASS')
                  AS qualified_until
              FROM federation.registrations r JOIN federation.registration_versions v
                ON v.tenant_id=r.tenant_id AND v.id=r.binding_version
              WHERE r.tenant_id=:tenant AND r.id=:agent FOR UPDATE OF r"""),
                {**params, "agent": agent_id},
            )
            .mappings()
            .one_or_none()
        )
        if (
            agent is None
            or agent["state"] != "active"
            or agent["registration"]["capability"] != capability_id
            or agent["qualified_until"] is None
        ):
            raise PermissionError("Provider is not active and qualified for this capability")
        authorization = self.session.execute(
            text("""SELECT id FROM federation.research_authorizations
              WHERE tenant_id=:tenant AND project_id=:project AND agent_id=:agent
                AND organization_id=:organization AND binding_version=:binding
                AND capability_id=:capability AND method_id=:method
                AND revoked_at IS NULL AND expires_at>clock_timestamp()
                AND :qualified_until>clock_timestamp()"""),
            {
                **params,
                "agent": agent_id,
                "organization": agent["organization_id"],
                "binding": agent["binding_version"],
                "capability": capability_id,
                "method": method_id,
                "qualified_until": agent["qualified_until"],
            },
        ).scalar_one_or_none()
        if authorization is None:
            raise PermissionError("Current research delegation approval is missing")

        reservation = self.session.execute(
            text("""SELECT amount FROM research.run_budget_reservations
              WHERE tenant_id=:tenant AND project_id=:project AND run_id=:run
                AND state='reserved' FOR UPDATE"""),
            params,
        ).scalar_one_or_none()
        if reservation is None:
            raise ValueError("Run budget reservation is not active")
        allocated = self.session.execute(
            text("""SELECT COALESCE(sum(budget_usd),0) FROM federation.research_dispatches
              WHERE tenant_id=:tenant AND project_id=:project AND run_id=:run
                AND (state IN ('queued','leased','completed','reconciliation_required')
                  OR signed_grant IS NOT NULL)"""),
            params,
        ).scalar_one()
        if allocated + budget_usd > reservation:
            raise ValueError("Remote delegation exceeds the reserved run budget")
        identifier = uuid4()
        self.session.execute(
            text("""INSERT INTO federation.research_dispatches
              (id,tenant_id,organization_id,project_id,run_id,agent_id,binding_version,
               authorization_id,capability_id,method_id,plan_digest,dataset_ids,budget_usd,
               request_digest,idempotency_key,created_by)
              VALUES(:id,:tenant,:organization,:project,:run,:agent,:binding,:authorization,
                :capability,:method,:plan_digest,:datasets,:budget,:request,:key,:actor)"""),
            {
                **params,
                "id": identifier,
                "organization": agent["organization_id"],
                "agent": agent_id,
                "binding": agent["binding_version"],
                "authorization": authorization,
                "capability": capability_id,
                "method": method_id,
                "plan_digest": run["plan_digest"],
                "datasets": plan.source_ids,
                "budget": budget_usd,
                "request": request_digest,
                "key": idempotency_key,
                "actor": self.principal.subject,
            },
        )
        return DispatchReceipt(identifier, run_id, agent_id, "queued")
