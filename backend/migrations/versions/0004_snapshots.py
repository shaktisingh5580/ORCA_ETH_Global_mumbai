"""Purpose: persist tenant-owned immutable P03 snapshot manifests.
Behavior: bind snapshots to tenant/version keys and enforce append-only RLS records.
Refs: main plan P03.3; research-only guide 4.5/5.2; Phase: P03.3.
"""

from alembic import op

revision = "0004_snapshots"
down_revision = "0003_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE research.catalog_versions ADD CONSTRAINT catalog_versions_tenant_id_unique UNIQUE(tenant_id,id)"
    )
    op.execute("""CREATE TABLE research.dataset_snapshots (
      id uuid PRIMARY KEY, tenant_id uuid NOT NULL, version_id uuid NOT NULL,
      raw_content_digest text NOT NULL CHECK(raw_content_digest ~ '^sha256:[0-9a-f]{64}$'),
      byte_count bigint NOT NULL CHECK(byte_count>0), manifest jsonb NOT NULL
        CHECK(jsonb_typeof(manifest)='object'),
      created_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY(tenant_id,version_id) REFERENCES research.catalog_versions(tenant_id,id))""")
    op.execute(
        "CREATE INDEX ix_dataset_snapshots_tenant_id ON research.dataset_snapshots(tenant_id,id)"
    )
    op.execute("ALTER TABLE research.dataset_snapshots ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE research.dataset_snapshots FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY dataset_snapshots_tenant ON research.dataset_snapshots
      USING (tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
      WITH CHECK (tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT, INSERT ON research.dataset_snapshots TO orca_runtime")
    op.execute("""CREATE TRIGGER dataset_snapshots_immutable BEFORE UPDATE OR DELETE
      ON research.dataset_snapshots FOR EACH ROW EXECUTE FUNCTION
      research.reject_catalog_version_mutation()""")


def downgrade() -> None:
    raise RuntimeError("Snapshot downgrade requires explicit evidence-retention review")
