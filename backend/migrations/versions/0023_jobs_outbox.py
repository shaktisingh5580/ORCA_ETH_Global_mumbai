"""Purpose: persist P05 transactional runs, leases, fencing, reservations and an outbox.
Behavior: tenant RLS and constraints prevent unapproved enqueue, duplicate jobs and overspending records.
Refs: main_implementation plan.md P05.2; api design.md §§5.1, 18.1; Phase: P05.
"""

from alembic import op

revision = "0023_jobs_outbox"
down_revision = "0022_plan_approvals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE research.compute_runs (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,investigation_id uuid NOT NULL,
      plan_id uuid NOT NULL,plan_digest text NOT NULL CHECK(plan_digest ~ '^sha256:[0-9a-f]{64}$'),
      reservation_id uuid,created_by varchar(255) NOT NULL,execution_budget numeric(20,8) NOT NULL CHECK(execution_budget>0),
      request_digest text NOT NULL CHECK(request_digest ~ '^sha256:[0-9a-f]{64}$'),idempotency_key varchar(128) NOT NULL,
      state varchar(32) NOT NULL CHECK(state IN ('QUEUED','RUNNING','CANCEL_REQUESTED','CANCELLED','FAILED','SUCCEEDED_SUPPORTED','SUCCEEDED_INSUFFICIENT','INCONCLUSIVE')),
      stage varchar(64) NOT NULL,generation bigint NOT NULL DEFAULT 0 CHECK(generation>=0),
      created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,project_id,id),UNIQUE(tenant_id,project_id,investigation_id,idempotency_key),
      FOREIGN KEY(tenant_id,project_id,investigation_id) REFERENCES research.investigations(tenant_id,project_id,id),
      FOREIGN KEY(tenant_id,project_id,investigation_id,plan_id)
        REFERENCES research.investigation_plans(tenant_id,project_id,investigation_id,id))""")
    op.execute("""CREATE TABLE research.plan_budget_ledgers (
      tenant_id uuid NOT NULL,project_id uuid NOT NULL,plan_id uuid NOT NULL,
      limit_amount numeric(20,8) NOT NULL CHECK(limit_amount>0),reserved_amount numeric(20,8) NOT NULL DEFAULT 0
        CHECK(reserved_amount>=0 AND reserved_amount<=limit_amount),updated_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(tenant_id,project_id,plan_id),
      FOREIGN KEY(tenant_id,project_id,plan_id) REFERENCES research.investigation_plans(tenant_id,project_id,id))""")
    op.execute("""CREATE TABLE research.run_budget_reservations (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,run_id uuid NOT NULL,plan_id uuid NOT NULL,
      amount numeric(20,8) NOT NULL CHECK(amount>0),state varchar(16) NOT NULL CHECK(state IN ('reserved','released','settled')),
      created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,project_id,id),UNIQUE(tenant_id,project_id,run_id),
      FOREIGN KEY(tenant_id,project_id,run_id) REFERENCES research.compute_runs(tenant_id,project_id,id),
      FOREIGN KEY(tenant_id,project_id,plan_id) REFERENCES research.investigation_plans(tenant_id,project_id,id))""")
    op.execute(
        "ALTER TABLE research.compute_runs ADD CONSTRAINT compute_runs_reservation_fk FOREIGN KEY(tenant_id,project_id,reservation_id) REFERENCES research.run_budget_reservations(tenant_id,project_id,id) DEFERRABLE INITIALLY DEFERRED"
    )
    op.execute("""CREATE TABLE research.job_queue (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,run_id uuid NOT NULL,payload jsonb NOT NULL,
      state varchar(16) NOT NULL CHECK(state IN ('queued','leased','completed','cancelled')),
      generation bigint NOT NULL DEFAULT 0 CHECK(generation>=0),attempt_id uuid,lease_token uuid,lease_until timestamptz,
      available_at timestamptz NOT NULL DEFAULT now(),created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,project_id,id),UNIQUE(tenant_id,run_id),
      FOREIGN KEY(tenant_id,project_id,run_id) REFERENCES research.compute_runs(tenant_id,project_id,id),
      CHECK((state='leased')=(attempt_id IS NOT NULL AND lease_token IS NOT NULL AND lease_until IS NOT NULL)))""")
    op.execute("""CREATE TABLE research.job_attempts (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,job_id uuid NOT NULL,run_id uuid NOT NULL,
      generation bigint NOT NULL CHECK(generation>0),lease_token uuid NOT NULL,lease_until timestamptz,state varchar(16) NOT NULL
        CHECK(state IN ('leased','expired','completed','cancelled')),claimed_at timestamptz NOT NULL DEFAULT now(),finished_at timestamptz,
      UNIQUE(tenant_id,project_id,id),UNIQUE(tenant_id,job_id,generation),
      FOREIGN KEY(tenant_id,project_id,job_id) REFERENCES research.job_queue(tenant_id,project_id,id),
      FOREIGN KEY(tenant_id,project_id,run_id) REFERENCES research.compute_runs(tenant_id,project_id,id))""")
    op.execute("""CREATE TABLE research.job_outbox (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,run_id uuid NOT NULL,event_type varchar(96) NOT NULL,
      payload jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,project_id,id),UNIQUE(tenant_id,run_id,event_type),
      FOREIGN KEY(tenant_id,project_id,run_id) REFERENCES research.compute_runs(tenant_id,project_id,id))""")
    for table in (
        "compute_runs",
        "plan_budget_ledgers",
        "run_budget_reservations",
        "job_queue",
        "job_attempts",
        "job_outbox",
    ):
        op.execute(f"ALTER TABLE research.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE research.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON research.{table} USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid) WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)"
        )
        op.execute(f"GRANT SELECT,INSERT,UPDATE ON research.{table} TO orca_runtime")
    op.execute(
        "CREATE INDEX job_queue_claim ON research.job_queue(tenant_id,project_id,state,available_at,lease_until)"
    )
    op.execute(
        "CREATE INDEX job_attempts_active ON research.job_attempts(tenant_id,job_id,state,lease_until)"
    )


def downgrade() -> None:
    raise RuntimeError("Durable run jobs and reservation history require explicit recovery review")
