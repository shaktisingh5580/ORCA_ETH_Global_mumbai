"""Purpose: persist tenant-scoped P08 research run admission quotas.
Behavior: RLS protects versioned limits and existing organizations use a conservative default.
Refs: main_implementation plan.md P08.1; api design.md §3.9; Phase: P08.
"""

from alembic import op

revision = "0033_run_quotas"
down_revision = "0032_benchmark_reference_pin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE iam.organization_run_quotas (
      tenant_id uuid PRIMARY KEY REFERENCES iam.organizations(tenant_id) ON DELETE CASCADE,
      max_runs_per_minute integer NOT NULL CHECK(max_runs_per_minute BETWEEN 1 AND 100),
      version integer NOT NULL DEFAULT 1 CHECK(version>0),
      updated_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("ALTER TABLE iam.organization_run_quotas ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE iam.organization_run_quotas FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY organization_run_quotas_tenant ON iam.organization_run_quotas
      USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
      WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT,INSERT,UPDATE ON iam.organization_run_quotas TO orca_runtime")
    op.execute("""CREATE INDEX compute_runs_tenant_created_at
      ON research.compute_runs(tenant_id,created_at DESC)""")


def downgrade() -> None:
    raise RuntimeError("Organization quota history requires explicit retention review")
