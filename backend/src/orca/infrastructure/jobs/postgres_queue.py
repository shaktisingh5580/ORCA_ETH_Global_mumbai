"""Purpose: provide P05's PostgreSQL-backed run queue, leases, fencing and reservations.
Behavior: atomically enqueue approved runs, reserve plan budget and reject stale worker completion.
Refs: main_implementation plan.md P05.2; api design.md §§5.1, 18.1; Phase: P05.
"""

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.application.projects import IdempotencyConflict


@dataclass(frozen=True, slots=True)
class EnqueuedRun:
    """Validated internal enqueue request; authorization belongs to the calling application service."""

    investigation_id: UUID
    plan_id: UUID
    approved_digest: str
    plan_budget: Decimal
    execution_budget: Decimal
    request_digest: str
    idempotency_key: str
    actor: str
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class QueueReceipt:
    run_id: UUID
    job_id: UUID
    reservation_id: UUID
    state: str


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    job_id: UUID
    run_id: UUID
    attempt_id: UUID
    lease_token: UUID
    generation: int
    payload: dict[str, Any]


class PostgresQueue:
    """Tenant-bound queue; every mutation must execute inside one TenantUnitOfWork."""

    def __init__(self, session: Session, tenant: UUID, project: UUID):
        self.session, self.tenant, self.project = session, tenant, project

    def enqueue_run(self, command: EnqueuedRun) -> QueueReceipt:
        """Insert run, reservation, queue record and outbox notification atomically."""
        if not 1 <= len(command.idempotency_key) <= 128:
            raise ValueError("invalid idempotency key")
        if command.execution_budget <= 0 or command.plan_budget <= 0:
            raise ValueError("execution and plan budgets must be positive")
        if command.execution_budget > command.plan_budget:
            raise ValueError("execution budget exceeds approved plan budget")
        if not command.approved_digest.startswith("sha256:") or len(command.approved_digest) != 71:
            raise ValueError("approved digest is invalid")

        existing = (
            self.session.execute(
                text("""SELECT id,request_digest,reservation_id FROM research.compute_runs
                  WHERE tenant_id=:tenant AND project_id=:project AND investigation_id=:investigation
                    AND idempotency_key=:key"""),
                {
                    "tenant": self.tenant,
                    "project": self.project,
                    "investigation": command.investigation_id,
                    "key": command.idempotency_key,
                },
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if existing["request_digest"] != command.request_digest:
                raise IdempotencyConflict()
            job = (
                self.session.execute(
                    text(
                        "SELECT id,state FROM research.job_queue WHERE tenant_id=:tenant AND run_id=:run"
                    ),
                    {"tenant": self.tenant, "run": existing["id"]},
                )
                .mappings()
                .one()
            )
            return QueueReceipt(existing["id"], job["id"], existing["reservation_id"], job["state"])

        approved = self.session.execute(
            text("""SELECT 1 FROM research.plan_approvals
              WHERE tenant_id=:tenant AND project_id=:project AND investigation_id=:investigation
                AND plan_id=:plan AND plan_digest=:digest"""),
            {
                "tenant": self.tenant,
                "project": self.project,
                "investigation": command.investigation_id,
                "plan": command.plan_id,
                "digest": command.approved_digest,
            },
        ).scalar_one_or_none()
        if approved is None:
            raise ValueError("exact approved plan digest is required before enqueue")

        run_id, reservation_id, job_id = uuid4(), uuid4(), uuid4()
        self.session.execute(
            text("""INSERT INTO research.compute_runs
              (id,tenant_id,project_id,investigation_id,plan_id,plan_digest,created_by,
               execution_budget,request_digest,idempotency_key,state,stage)
              VALUES(:id,:tenant,:project,:investigation,:plan,:digest,:actor,:budget,:request,:key,
                'QUEUED','queue')"""),
            {
                "id": run_id,
                "tenant": self.tenant,
                "project": self.project,
                "investigation": command.investigation_id,
                "plan": command.plan_id,
                "digest": command.approved_digest,
                "actor": command.actor,
                "budget": command.execution_budget,
                "request": command.request_digest,
                "key": command.idempotency_key,
            },
        )
        self.session.execute(
            text("""INSERT INTO research.plan_budget_ledgers
              (tenant_id,project_id,plan_id,limit_amount,reserved_amount)
              VALUES(:tenant,:project,:plan,:limit,0)
              ON CONFLICT(tenant_id,project_id,plan_id) DO NOTHING"""),
            {
                "tenant": self.tenant,
                "project": self.project,
                "plan": command.plan_id,
                "limit": command.plan_budget,
            },
        )
        reserved = self.session.execute(
            text("""UPDATE research.plan_budget_ledgers SET reserved_amount=reserved_amount+:amount,
              updated_at=clock_timestamp()
              WHERE tenant_id=:tenant AND project_id=:project AND plan_id=:plan
                AND reserved_amount+:amount<=limit_amount RETURNING plan_id"""),
            {
                "tenant": self.tenant,
                "project": self.project,
                "plan": command.plan_id,
                "amount": command.execution_budget,
            },
        ).scalar_one_or_none()
        if reserved is None:
            raise ValueError("approved plan budget is already reserved")
        self.session.execute(
            text("""INSERT INTO research.run_budget_reservations
              (id,tenant_id,project_id,run_id,plan_id,amount,state)
              VALUES(:id,:tenant,:project,:run,:plan,:amount,'reserved')"""),
            {
                "id": reservation_id,
                "tenant": self.tenant,
                "project": self.project,
                "run": run_id,
                "plan": command.plan_id,
                "amount": command.execution_budget,
            },
        )
        self.session.execute(
            text("""UPDATE research.compute_runs SET reservation_id=:reservation
              WHERE tenant_id=:tenant AND id=:run"""),
            {"tenant": self.tenant, "run": run_id, "reservation": reservation_id},
        )
        self.session.execute(
            text("""INSERT INTO research.job_queue
              (id,tenant_id,project_id,run_id,payload,state)
              VALUES(:id,:tenant,:project,:run,CAST(:payload AS jsonb),'queued')"""),
            {
                "id": job_id,
                "tenant": self.tenant,
                "project": self.project,
                "run": run_id,
                "payload": json.dumps(command.payload, sort_keys=True, separators=(",", ":")),
            },
        )
        self.session.execute(
            text("""INSERT INTO research.job_outbox
              (id,tenant_id,project_id,run_id,event_type,payload)
              VALUES(:id,:tenant,:project,:run,'investigation.run.queued',CAST(:payload AS jsonb))"""),
            {
                "id": uuid4(),
                "tenant": self.tenant,
                "project": self.project,
                "run": run_id,
                "payload": json.dumps({"job_id": str(job_id), "run_id": str(run_id)}),
            },
        )
        return QueueReceipt(run_id, job_id, reservation_id, "queued")

    def claim(self, *, lease_seconds: int = 120) -> ClaimedJob | None:
        """Claim one queued or expired lease using database time and row locks."""
        if not 1 <= lease_seconds <= 900:
            raise ValueError("lease duration must be from 1 through 900 seconds")
        row = (
            self.session.execute(
                text("""WITH candidate AS (
              SELECT id FROM research.job_queue WHERE tenant_id=:tenant AND project_id=:project
                AND (state='queued' OR (state='leased' AND lease_until<clock_timestamp()))
              ORDER BY available_at,created_at,id FOR UPDATE SKIP LOCKED LIMIT 1)
              UPDATE research.job_queue q SET state='leased',generation=q.generation+1,
                attempt_id=:attempt,lease_token=:token,
                lease_until=clock_timestamp()+make_interval(secs=>:seconds),updated_at=clock_timestamp()
              FROM candidate c WHERE q.id=c.id AND q.tenant_id=:tenant
              RETURNING q.id,q.run_id,q.attempt_id,q.lease_token,q.generation,q.payload"""),
                {
                    "tenant": self.tenant,
                    "project": self.project,
                    "attempt": uuid4(),
                    "token": uuid4(),
                    "seconds": lease_seconds,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        self.session.execute(
            text("""UPDATE research.job_attempts SET state='expired',finished_at=clock_timestamp()
              WHERE tenant_id=:tenant AND job_id=:job AND state='leased'"""),
            {"tenant": self.tenant, "job": row["id"]},
        )
        self.session.execute(
            text("""INSERT INTO research.job_attempts
              (id,tenant_id,project_id,job_id,run_id,generation,lease_token,lease_until,state)
              SELECT :attempt,:tenant,:project,id,run_id,:generation,:token,lease_until,'leased'
              FROM research.job_queue WHERE tenant_id=:tenant AND id=:job"""),
            {
                "attempt": row["attempt_id"],
                "tenant": self.tenant,
                "project": self.project,
                "job": row["id"],
                "generation": row["generation"],
                "token": row["lease_token"],
            },
        )
        self.session.execute(
            text("""UPDATE research.compute_runs SET state='RUNNING',stage='leased',updated_at=clock_timestamp()
              WHERE tenant_id=:tenant AND id=:run AND state='QUEUED'"""),
            {"tenant": self.tenant, "run": row["run_id"]},
        )
        return ClaimedJob(
            row["id"],
            row["run_id"],
            row["attempt_id"],
            row["lease_token"],
            row["generation"],
            row["payload"],
        )

    def renew(self, claimed: ClaimedJob, *, lease_seconds: int = 120) -> bool:
        if not 1 <= lease_seconds <= 900:
            raise ValueError("lease duration must be from 1 through 900 seconds")
        changed = self.session.execute(
            text("""UPDATE research.job_queue SET lease_until=clock_timestamp()+make_interval(secs=>:seconds),
              updated_at=clock_timestamp() WHERE tenant_id=:tenant AND id=:job AND state='leased'
              AND attempt_id=:attempt AND lease_token=:token AND generation=:generation
              AND lease_until>clock_timestamp() RETURNING id"""),
            {
                "tenant": self.tenant,
                "job": claimed.job_id,
                "attempt": claimed.attempt_id,
                "token": claimed.lease_token,
                "generation": claimed.generation,
                "seconds": lease_seconds,
            },
        ).scalar_one_or_none()
        if changed is None:
            return False
        self.session.execute(
            text("""UPDATE research.job_attempts SET lease_until=(SELECT lease_until FROM research.job_queue
              WHERE tenant_id=:tenant AND id=:job) WHERE tenant_id=:tenant AND id=:attempt
              AND state='leased' AND lease_token=:token AND generation=:generation"""),
            {
                "tenant": self.tenant,
                "job": claimed.job_id,
                "attempt": claimed.attempt_id,
                "token": claimed.lease_token,
                "generation": claimed.generation,
            },
        )
        return True

    def complete(self, claimed: ClaimedJob) -> bool:
        """Close only the queue attempt; P05.3 publishes a scientific outcome separately."""
        changed = self.session.execute(
            text("""UPDATE research.job_queue SET state='completed',attempt_id=NULL,lease_token=NULL,lease_until=NULL,
              updated_at=clock_timestamp() WHERE tenant_id=:tenant AND id=:job AND state='leased'
              AND attempt_id=:attempt AND lease_token=:token AND generation=:generation
              AND lease_until>clock_timestamp() RETURNING id"""),
            {
                "tenant": self.tenant,
                "job": claimed.job_id,
                "attempt": claimed.attempt_id,
                "token": claimed.lease_token,
                "generation": claimed.generation,
            },
        ).scalar_one_or_none()
        if changed is None:
            return False
        self.session.execute(
            text("""UPDATE research.job_attempts SET state='completed',finished_at=clock_timestamp(),lease_until=NULL
              WHERE tenant_id=:tenant AND id=:attempt AND lease_token=:token AND generation=:generation"""),
            {
                "tenant": self.tenant,
                "attempt": claimed.attempt_id,
                "token": claimed.lease_token,
                "generation": claimed.generation,
            },
        )
        return True

    def finish_inconclusive(self, claimed: ClaimedJob, reason: str) -> bool:
        """Fence an unavailable prerequisite into a durable non-success terminal state."""
        if not 1 <= len(reason) <= 160:
            raise ValueError("bounded terminal reason is required")
        changed = self.session.execute(
            text("""UPDATE research.compute_runs SET state='INCONCLUSIVE',stage=:stage,
              updated_at=clock_timestamp() WHERE tenant_id=:tenant AND id=:run
              AND state IN ('QUEUED','RUNNING','CANCEL_REQUESTED') RETURNING id"""),
            {"tenant": self.tenant, "run": claimed.run_id, "stage": f"blocked:{reason}"},
        ).scalar_one_or_none()
        return changed is not None and self.complete(claimed)

    def cancel(self, job_id: UUID) -> bool:
        """Fence a current worker before marking the queue record cancelled."""
        changed = self.session.execute(
            text("""UPDATE research.job_queue SET state='cancelled',generation=generation+1,attempt_id=NULL,
              lease_token=NULL,lease_until=NULL,updated_at=clock_timestamp() WHERE tenant_id=:tenant AND id=:job
              AND state IN ('queued','leased') RETURNING run_id"""),
            {"tenant": self.tenant, "job": job_id},
        ).scalar_one_or_none()
        if changed is None:
            return False
        self.session.execute(
            text("""UPDATE research.job_attempts SET state='cancelled',finished_at=clock_timestamp(),lease_until=NULL
              WHERE tenant_id=:tenant AND job_id=:job AND state='leased'"""),
            {"tenant": self.tenant, "job": job_id},
        )
        self.session.execute(
            text("""UPDATE research.compute_runs SET state='CANCEL_REQUESTED',stage='cancel_requested',
              updated_at=clock_timestamp() WHERE tenant_id=:tenant AND id=:run AND state IN ('QUEUED','RUNNING')"""),
            {"tenant": self.tenant, "run": changed},
        )
        return True
