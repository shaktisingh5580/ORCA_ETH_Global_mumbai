"""Purpose: persist the bounded hackathon investigation, run and publication workflow.
Behavior: use additive project/tenant keys, RLS and durable run claims without a production DAG.
Refs: HACKATHON_IMPLEMENTATION_PLAN.md H02/H04; API 3.6 guidance; Phase: H02.
"""

from alembic import op

revision = "0013_prototype_workflow"
down_revision = "0012_invitations_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE research.prototype_investigations (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,
      created_by varchar(255) NOT NULL,question text NOT NULL,plan jsonb NOT NULL,
      state varchar(16) NOT NULL DEFAULT 'planned' CHECK(state IN ('planned','approved')),
      approved_by varchar(255),created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),UNIQUE(tenant_id,id,project_id),
      FOREIGN KEY(tenant_id,project_id) REFERENCES research.projects(tenant_id,id))""")
    op.execute("""CREATE TABLE research.prototype_runs (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,investigation_id uuid NOT NULL,
      created_by varchar(255) NOT NULL,idempotency_key varchar(128) NOT NULL,bundle_digest text NOT NULL,
      status varchar(16) NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','succeeded','failed')),
      progress varchar(80) NOT NULL DEFAULT 'queued',attempts integer NOT NULL DEFAULT 0,
      worker_token uuid,lease_until timestamptz,result jsonb,result_digest text,report text,error_code varchar(80),
      created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,id),UNIQUE(tenant_id,investigation_id,idempotency_key),
      FOREIGN KEY(tenant_id,investigation_id,project_id) REFERENCES research.prototype_investigations(tenant_id,id,project_id),
      CHECK(bundle_digest ~ '^sha256:[0-9a-f]{64}$'),CHECK(result_digest IS NULL OR result_digest ~ '^sha256:[0-9a-f]{64}$'))""")
    op.execute("""CREATE TABLE research.prototype_publications (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,run_id uuid NOT NULL,
      status varchar(24) NOT NULL DEFAULT 'submission_started',receipt jsonb,
      created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(tenant_id,run_id),
      FOREIGN KEY(tenant_id,run_id) REFERENCES research.prototype_runs(tenant_id,id))""")
    for table in ("prototype_investigations", "prototype_runs", "prototype_publications"):
        op.execute(f"ALTER TABLE research.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE research.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY {table}_tenant ON research.{table}
          USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
          WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
        op.execute(f"GRANT SELECT,INSERT,UPDATE ON research.{table} TO orca_runtime")
    op.execute("""CREATE FUNCTION iam.prototype_pending_runs() RETURNS TABLE(tenant_id uuid,run_id uuid)
      LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,research AS $$
      SELECT r.tenant_id,r.id FROM research.prototype_runs r WHERE r.status='queued'
      OR (r.status='running' AND r.lease_until<clock_timestamp()) ORDER BY r.created_at LIMIT 20 $$""")
    op.execute("REVOKE ALL ON FUNCTION iam.prototype_pending_runs() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION iam.prototype_pending_runs() TO orca_runtime")
    op.execute("CREATE INDEX prototype_run_queue ON research.prototype_runs(status,created_at)")
    op.execute("""CREATE FUNCTION research.protect_prototype_result() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN IF OLD.status='succeeded' THEN RAISE EXCEPTION 'completed prototype results are immutable'; END IF;
      RETURN NEW; END $$""")
    op.execute("""CREATE TRIGGER prototype_result_immutable BEFORE UPDATE OR DELETE ON research.prototype_runs
      FOR EACH ROW EXECUTE FUNCTION research.protect_prototype_result()""")


def downgrade() -> None:
    raise RuntimeError("Prototype data removal requires explicit retention review")
