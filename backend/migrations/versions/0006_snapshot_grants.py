"""Purpose: bind snapshot use to explicit tenant/project grants.
Behavior: enforce composite ownership, expiry and tenant RLS without copying snapshot bytes.
Refs: API 3.4; main plan P03.5; Phase: P03.
"""

from alembic import op

revision = "0006_snapshot_grants"
down_revision = "0005_snapshot_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE research.snapshot_project_grants (
      tenant_id uuid NOT NULL, project_id uuid NOT NULL, snapshot_id uuid NOT NULL,
      expires_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(tenant_id,project_id,snapshot_id),
      FOREIGN KEY(tenant_id,project_id) REFERENCES research.projects(tenant_id,id) ON DELETE CASCADE,
      FOREIGN KEY(tenant_id,snapshot_id) REFERENCES research.dataset_snapshots(tenant_id,id))""")
    op.execute("ALTER TABLE research.snapshot_project_grants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE research.snapshot_project_grants FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY snapshot_grants_tenant ON research.snapshot_project_grants
      USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
      WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
    op.execute(
        "GRANT SELECT,INSERT,UPDATE,DELETE ON research.snapshot_project_grants TO orca_runtime"
    )


def downgrade() -> None:
    raise RuntimeError("Grant downgrade requires explicit access-policy review")
