"""Purpose: add recoverable project-scoped model planning and approval records.
Behavior: fence worker leases, preserve event sequences and restrict tenant access.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A02; Phase: A02.
"""

from alembic import op

revision = "0016_reasoning_runs"
down_revision = "0015_tabular_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE research.reasoning_runs (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,created_by text NOT NULL,
      request jsonb NOT NULL,request_digest text NOT NULL,idempotency_key text NOT NULL,
      state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','planning','planned','blocked','failed','completed')),
      plan jsonb,plan_digest text,result jsonb,error_code text,approved_by text,
      lease_token uuid,lease_until timestamptz,attempts integer NOT NULL DEFAULT 0,
      created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,project_id,id),UNIQUE(tenant_id,project_id,idempotency_key),
      FOREIGN KEY(tenant_id,project_id) REFERENCES research.projects(tenant_id,id))""")
    op.execute("""CREATE TABLE research.reasoning_events (
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,
      run_id uuid NOT NULL,event_type text NOT NULL,data jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY(tenant_id,project_id,run_id) REFERENCES research.reasoning_runs(tenant_id,project_id,id))""")
    for table in ("reasoning_runs", "reasoning_events"):
        op.execute(f"ALTER TABLE research.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE research.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY {table}_tenant ON research.{table}
          USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
          WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
        op.execute(f"GRANT SELECT,INSERT ON research.{table} TO orca_runtime")
    op.execute("GRANT UPDATE ON research.reasoning_runs TO orca_runtime")
    op.execute("GRANT USAGE ON SEQUENCE research.reasoning_events_id_seq TO orca_runtime")
    op.execute("""CREATE TRIGGER reasoning_events_immutable BEFORE UPDATE OR DELETE ON research.reasoning_events
      FOR EACH ROW EXECUTE FUNCTION research.protect_tabular_evidence()""")
    op.execute("""CREATE FUNCTION iam.reasoning_pending_runs() RETURNS TABLE(tenant_id uuid,run_id uuid)
      LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,research AS $$
      SELECT r.tenant_id,r.id FROM research.reasoning_runs r WHERE r.state='queued'
      OR (r.state='planning' AND r.lease_until<clock_timestamp()) ORDER BY r.created_at LIMIT 10 $$""")
    op.execute("REVOKE ALL ON FUNCTION iam.reasoning_pending_runs() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION iam.reasoning_pending_runs() TO orca_runtime")


def downgrade() -> None:
    raise RuntimeError("Reasoning history removal requires explicit retention review")
