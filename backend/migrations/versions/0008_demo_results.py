"""Purpose: retain immutable descriptive research results for the accelerated Arkiv demo.
Behavior: enforce tenant snapshot ownership, RLS and append-only result records.
Refs: accelerated-research-arkiv-demo.json; Phase: accelerated P04/P06 demo.
"""

from alembic import op

revision = "0008_demo_results"
down_revision = "0007_source_probes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE research.demo_results (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,snapshot_id uuid NOT NULL,
      result_digest text NOT NULL CHECK(result_digest ~ '^sha256:[0-9a-f]{64}$'),manifest jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(tenant_id,id),
      FOREIGN KEY(tenant_id,snapshot_id) REFERENCES research.dataset_snapshots(tenant_id,id))""")
    op.execute("ALTER TABLE research.demo_results ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE research.demo_results FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY demo_results_tenant ON research.demo_results
      USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
      WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT,INSERT ON research.demo_results TO orca_runtime")
    op.execute("""CREATE TRIGGER demo_results_immutable BEFORE UPDATE OR DELETE ON research.demo_results
      FOR EACH ROW EXECUTE FUNCTION research.reject_catalog_version_mutation()""")


def downgrade() -> None:
    raise RuntimeError("Demo evidence downgrade requires explicit retention review")
