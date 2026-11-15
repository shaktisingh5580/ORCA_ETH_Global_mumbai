"""Purpose: persist private investigations and immutable typed plan revisions.
Behavior: enforce tenant/project keys, RLS, idempotency and append-only plan contents.
Refs: main_implementation plan.md P05.1; api design.md §§3.6, 5.1; Phase: P05.
"""

from alembic import op

revision = "0021_investigations"
down_revision = "0020_science_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE research.investigations (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,
      created_by varchar(255) NOT NULL,question text NOT NULL,description text,
      geo_scope jsonb NOT NULL CHECK(jsonb_typeof(geo_scope)='object'),
      time_window jsonb NOT NULL CHECK(jsonb_typeof(time_window)='object'),
      as_of timestamptz,risk_tier varchar(64) NOT NULL,
      state varchar(24) NOT NULL DEFAULT 'DRAFT'
        CHECK(state IN ('DRAFT','PLAN_PENDING','PLAN_READY','APPROVED')),
      current_plan_revision integer NOT NULL DEFAULT 0 CHECK(current_plan_revision>=0),
      version bigint NOT NULL DEFAULT 1 CHECK(version>0),
      idempotency_key varchar(128) NOT NULL,request_digest text NOT NULL
        CHECK(request_digest ~ '^sha256:[0-9a-f]{64}$'),
      created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,project_id,id),UNIQUE(tenant_id,project_id,idempotency_key),
      FOREIGN KEY(tenant_id,project_id) REFERENCES research.projects(tenant_id,id) ON DELETE CASCADE,
      CHECK(length(question) BETWEEN 10 AND 2000),CHECK(length(risk_tier) BETWEEN 1 AND 64))""")
    op.execute("""CREATE TABLE research.investigation_plans (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,investigation_id uuid NOT NULL,
      revision integer NOT NULL CHECK(revision>0),plan jsonb NOT NULL CHECK(jsonb_typeof(plan)='object'),
      plan_digest text NOT NULL CHECK(plan_digest ~ '^sha256:[0-9a-f]{64}$'),
      request_digest text NOT NULL CHECK(request_digest ~ '^sha256:[0-9a-f]{64}$'),
      idempotency_key varchar(128) NOT NULL,created_by varchar(255) NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,project_id,id),UNIQUE(tenant_id,project_id,investigation_id,revision),
      UNIQUE(tenant_id,project_id,investigation_id,idempotency_key),
      FOREIGN KEY(tenant_id,project_id,investigation_id)
        REFERENCES research.investigations(tenant_id,project_id,id) ON DELETE CASCADE)""")
    for table in ("investigations", "investigation_plans"):
        op.execute(f"ALTER TABLE research.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE research.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY {table}_tenant ON research.{table}
          USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
          WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
        op.execute(f"GRANT SELECT,INSERT ON research.{table} TO orca_runtime")
    op.execute("GRANT UPDATE ON research.investigations TO orca_runtime")
    op.execute("""CREATE FUNCTION research.protect_investigation_plan() RETURNS trigger
      LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'investigation plan revisions are immutable'; END $$""")
    op.execute("""CREATE TRIGGER investigation_plans_immutable
      BEFORE UPDATE OR DELETE ON research.investigation_plans
      FOR EACH ROW EXECUTE FUNCTION research.protect_investigation_plan()""")
    op.execute(
        "CREATE INDEX investigations_project_created ON research.investigations(tenant_id,project_id,id)"
    )
    op.execute("""CREATE INDEX investigation_plans_by_revision
      ON research.investigation_plans(tenant_id,project_id,investigation_id,revision)""")
    op.execute("""CREATE FUNCTION iam.resolve_investigation_tenant(p_subject text,p_investigation uuid)
      RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,iam,research AS $$
      SELECT i.tenant_id FROM research.investigations i
      WHERE i.id=p_investigation
        AND iam.resolve_project_tenant(p_subject,i.project_id)=i.tenant_id LIMIT 1 $$""")
    op.execute("REVOKE ALL ON FUNCTION iam.resolve_investigation_tenant(text,uuid) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION iam.resolve_investigation_tenant(text,uuid) TO orca_runtime"
    )


def downgrade() -> None:
    raise RuntimeError("Investigation plans and approval history require explicit retention review")
