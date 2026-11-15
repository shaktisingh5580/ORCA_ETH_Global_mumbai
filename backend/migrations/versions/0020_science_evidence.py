"""Purpose: persist immutable P04 scientific method and snapshot lineage.
Behavior: enforce tenant/project scope, exact snapshot digest pins and append-only records.
Refs: main_implementation plan.md P04.4; docs/04_database.md §§3,7,9; Phase: P04.
"""

from alembic import op

revision = "0020_science_evidence"
down_revision = "0019_institutional_agents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE research.dataset_snapshots
      ADD CONSTRAINT dataset_snapshots_lineage_identity
      UNIQUE(tenant_id,id,raw_content_digest,byte_count)""")
    op.execute("""CREATE TABLE research.science_evidence_manifests (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,created_by text NOT NULL,
      manifest jsonb NOT NULL CHECK(jsonb_typeof(manifest)='object'),
      manifest_digest text NOT NULL CHECK(manifest_digest ~ '^sha256:[0-9a-f]{64}$'),
      created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(tenant_id,project_id,id),
      UNIQUE(tenant_id,project_id,manifest_digest),
      FOREIGN KEY(tenant_id,project_id) REFERENCES research.projects(tenant_id,id))""")
    op.execute("""CREATE TABLE research.science_evidence_inputs (
      tenant_id uuid NOT NULL,project_id uuid NOT NULL,manifest_id uuid NOT NULL,snapshot_id uuid NOT NULL,
      input_role text NOT NULL CHECK(input_role IN ('current','baseline')),
      raw_content_digest text NOT NULL CHECK(raw_content_digest ~ '^sha256:[0-9a-f]{64}$'),
      byte_count bigint NOT NULL CHECK(byte_count>0),
      PRIMARY KEY(tenant_id,project_id,manifest_id,input_role,snapshot_id),
      FOREIGN KEY(tenant_id,project_id,manifest_id)
        REFERENCES research.science_evidence_manifests(tenant_id,project_id,id),
      FOREIGN KEY(tenant_id,snapshot_id,raw_content_digest,byte_count)
        REFERENCES research.dataset_snapshots(tenant_id,id,raw_content_digest,byte_count))""")
    for table in ("science_evidence_manifests", "science_evidence_inputs"):
        op.execute(f"ALTER TABLE research.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE research.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY {table}_tenant ON research.{table}
          USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
          WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
        op.execute(f"GRANT SELECT,INSERT ON research.{table} TO orca_runtime")
        op.execute(f"""CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON research.{table}
          FOR EACH ROW EXECUTE FUNCTION research.protect_tabular_evidence()""")
    op.execute("""CREATE INDEX science_evidence_inputs_snapshot
      ON research.science_evidence_inputs(tenant_id,snapshot_id)""")


def downgrade() -> None:
    raise RuntimeError("Scientific evidence downgrade requires explicit retention review")
