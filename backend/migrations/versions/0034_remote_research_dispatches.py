"""Purpose: persist P09 remote research authorizations and fenced dispatch intents.
Behavior: bind tasks to approved run budgets and deny runtime writes to authorization policy.
Refs: main_implementation plan.md P09.3; api design.md §§2.2,17.1; Phase: P09.
"""

from alembic import op

revision = "0034_remote_research_dispatches"
down_revision = "0033_run_quotas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE federation.research_authorizations (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,organization_id uuid NOT NULL,
      project_id uuid NOT NULL,agent_id uuid NOT NULL,binding_version uuid NOT NULL,
      capability_id varchar(100) NOT NULL,method_id varchar(120) NOT NULL,
      expires_at timestamptz NOT NULL,approved_by text NOT NULL,
      revoked_at timestamptz,created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,project_id,agent_id,binding_version,id),
      UNIQUE(tenant_id,project_id,agent_id,binding_version,capability_id,method_id),
      FOREIGN KEY(tenant_id,project_id) REFERENCES research.projects(tenant_id,id),
      FOREIGN KEY(tenant_id,organization_id,agent_id)
        REFERENCES federation.registrations(tenant_id,organization_id,id),
      FOREIGN KEY(tenant_id,agent_id,binding_version)
        REFERENCES federation.registration_versions(tenant_id,agent_id,id))""")
    op.execute("""CREATE TABLE federation.research_dispatches (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,organization_id uuid NOT NULL,
      project_id uuid NOT NULL,run_id uuid NOT NULL,agent_id uuid NOT NULL,
      binding_version uuid NOT NULL,authorization_id uuid NOT NULL,
      capability_id varchar(100) NOT NULL,method_id varchar(120) NOT NULL,
      plan_digest text NOT NULL CHECK(plan_digest ~ '^sha256:[0-9a-f]{64}$'),
      dataset_ids uuid[] NOT NULL CHECK(array_length(dataset_ids,1) BETWEEN 1 AND 20),
      budget_usd numeric(20,8) NOT NULL CHECK(budget_usd>0),
      request_digest text NOT NULL CHECK(request_digest ~ '^sha256:[0-9a-f]{64}$'),
      idempotency_key varchar(128) NOT NULL,created_by text NOT NULL,
      state varchar(20) NOT NULL DEFAULT 'queued'
        CHECK(state IN ('queued','leased','completed','failed','cancelled')),
      generation bigint NOT NULL DEFAULT 0 CHECK(generation>=0),
      lease_token uuid,lease_until timestamptz,
      grant_digest text,result_digest text,error_code varchar(96),
      created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,project_id,id),UNIQUE(tenant_id,project_id,run_id,idempotency_key),
      FOREIGN KEY(tenant_id,project_id,run_id) REFERENCES research.compute_runs(tenant_id,project_id,id),
      FOREIGN KEY(tenant_id,organization_id,agent_id)
        REFERENCES federation.registrations(tenant_id,organization_id,id),
      FOREIGN KEY(tenant_id,project_id,agent_id,binding_version,authorization_id)
        REFERENCES federation.research_authorizations(tenant_id,project_id,agent_id,binding_version,id),
      CHECK((state='leased')=(lease_token IS NOT NULL AND lease_until IS NOT NULL)))""")
    for table in ("research_authorizations", "research_dispatches"):
        op.execute(f"ALTER TABLE federation.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE federation.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY {table}_tenant ON federation.{table}
          USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
          WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT ON federation.research_authorizations TO orca_runtime")
    op.execute("GRANT SELECT,INSERT,UPDATE ON federation.research_dispatches TO orca_runtime")
    op.execute("""CREATE INDEX research_dispatch_claim ON federation.research_dispatches
      (tenant_id,project_id,state,lease_until,created_at)""")


def downgrade() -> None:
    raise RuntimeError("Remote task and authorization history require explicit recovery review")
