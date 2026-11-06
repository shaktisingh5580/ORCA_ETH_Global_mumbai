"""Purpose: persist hackathon agent bindings, exclusive assignments and recoverable coordination.
Behavior: add tenant-keyed federation tables, forced RLS and immutable accepted evidence.
Refs: docs/04_database.md; H04-agent-coordination.json; Phase: H04.
"""

from alembic import op

revision = "0014_agent_coordination"
down_revision = "0013_prototype_workflow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS federation")
    op.execute("REVOKE CREATE ON SCHEMA federation FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA federation TO orca_runtime")
    op.execute(
        "ALTER TABLE research.prototype_runs ADD CONSTRAINT prototype_run_project_identity UNIQUE(tenant_id,id,project_id)"
    )
    op.execute("""CREATE TABLE federation.agents (
      id uuid NOT NULL,tenant_id uuid NOT NULL,binding_version uuid NOT NULL,
      binding jsonb NOT NULL,revoked boolean NOT NULL DEFAULT false,
      PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,binding_version),
      created_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("""CREATE TABLE federation.tasks (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,source_run_id uuid NOT NULL,
      capability varchar(32) NOT NULL CHECK(capability IN ('sst_reproduction','sst_review')),
      review_of uuid,publication_mode varchar(20) NOT NULL CHECK(publication_mode IN ('private','public_reference')),
      state varchar(24) NOT NULL DEFAULT 'open' CHECK(state IN ('open','assigned','running','completed','cancelled','failed','conflicted')),
      generation integer NOT NULL DEFAULT 0 CHECK(generation>=0),created_by varchar(255) NOT NULL,
      recipe jsonb NOT NULL,recipe_digest text NOT NULL CHECK(recipe_digest ~ '^sha256:[0-9a-f]{64}$'),
      deadline timestamptz NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,id),FOREIGN KEY(tenant_id,project_id) REFERENCES research.projects(tenant_id,id),
      FOREIGN KEY(tenant_id,source_run_id,project_id) REFERENCES research.prototype_runs(tenant_id,id,project_id),
      FOREIGN KEY(tenant_id,review_of) REFERENCES federation.tasks(tenant_id,id),
      CHECK((capability='sst_review')=(review_of IS NOT NULL)))""")
    op.execute("""CREATE TABLE federation.claims (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,task_id uuid NOT NULL,provider_id uuid NOT NULL,
      envelope jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,id,task_id,provider_id),UNIQUE(tenant_id,task_id,provider_id),
      FOREIGN KEY(tenant_id,task_id) REFERENCES federation.tasks(tenant_id,id),
      FOREIGN KEY(tenant_id,provider_id) REFERENCES federation.agents(tenant_id,id))""")
    op.execute("""CREATE TABLE federation.assignments (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,task_id uuid NOT NULL,generation integer NOT NULL CHECK(generation>0),
      claim_id uuid NOT NULL,provider_id uuid NOT NULL,attempt_id uuid NOT NULL,
      state varchar(20) NOT NULL DEFAULT 'active' CHECK(state IN ('active','completed','cancelled','expired','failed')),
      envelope jsonb,a2a_task_id varchar(128),a2a_context_id varchar(128),lease_until timestamptz NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(tenant_id,id),UNIQUE(tenant_id,task_id,generation),
      FOREIGN KEY(tenant_id,claim_id,task_id,provider_id) REFERENCES federation.claims(tenant_id,id,task_id,provider_id))""")
    op.execute(
        "CREATE UNIQUE INDEX one_active_assignment ON federation.assignments(tenant_id,task_id) WHERE state='active'"
    )
    op.execute("""CREATE TABLE federation.artifacts (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,task_id uuid NOT NULL,assignment_id uuid NOT NULL,
      provider_id uuid NOT NULL,envelope jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,assignment_id),
      FOREIGN KEY(tenant_id,task_id) REFERENCES federation.tasks(tenant_id,id),
      FOREIGN KEY(tenant_id,assignment_id) REFERENCES federation.assignments(tenant_id,id),
      FOREIGN KEY(tenant_id,provider_id) REFERENCES federation.agents(tenant_id,id))""")
    op.execute("""CREATE TABLE federation.events (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,task_id uuid NOT NULL,kind varchar(24) NOT NULL,
      actor_id uuid NOT NULL,envelope jsonb NOT NULL,publication_mode varchar(20) NOT NULL,
      receipt jsonb,created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(tenant_id,id),
      FOREIGN KEY(tenant_id,task_id) REFERENCES federation.tasks(tenant_id,id))""")
    op.execute("""CREATE TABLE federation.operations (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,task_id uuid NOT NULL,created_by varchar(255) NOT NULL,
      action varchar(24) NOT NULL,provider_id uuid,payload jsonb NOT NULL,request_digest text NOT NULL,
      idempotency_key varchar(128) NOT NULL,status varchar(32) NOT NULL DEFAULT 'queued'
      CHECK(status IN ('queued','running','succeeded','failed','reconciliation_required')),
      attempts integer NOT NULL DEFAULT 0,worker_token uuid,lease_until timestamptz,error_code varchar(80),
      created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,created_by,action,idempotency_key),FOREIGN KEY(tenant_id,task_id) REFERENCES federation.tasks(tenant_id,id),
      FOREIGN KEY(tenant_id,provider_id) REFERENCES federation.agents(tenant_id,id))""")
    for table in ("agents", "tasks", "claims", "assignments", "artifacts", "events", "operations"):
        op.execute(f"ALTER TABLE federation.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE federation.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY {table}_tenant ON federation.{table}
          USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
          WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
        op.execute(f"GRANT SELECT,INSERT,UPDATE ON federation.{table} TO orca_runtime")
    op.execute("""CREATE FUNCTION federation.immutable_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'accepted agent evidence is immutable'; END $$""")
    for table in ("claims", "artifacts", "events"):
        op.execute(f"""CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON federation.{table}
          FOR EACH ROW EXECUTE FUNCTION federation.immutable_evidence()""")
    op.execute("""CREATE FUNCTION iam.network_pending_operations() RETURNS TABLE(tenant_id uuid,operation_id uuid)
      LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,federation AS $$
      SELECT o.tenant_id,o.id FROM federation.operations o WHERE o.status='queued'
      OR (o.status='running' AND o.lease_until<clock_timestamp()) ORDER BY o.created_at LIMIT 20 $$""")
    op.execute("REVOKE ALL ON FUNCTION iam.network_pending_operations() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION iam.network_pending_operations() TO orca_runtime")


def downgrade() -> None:
    raise RuntimeError("Agent evidence removal requires explicit retention review")
