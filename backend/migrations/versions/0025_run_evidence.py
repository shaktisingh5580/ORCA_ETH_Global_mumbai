"""Purpose: persist P05 fenced checkpoints and immutable run-to-evidence publication.
Behavior: tie one final science manifest to a run without allowing later mutation or cross-tenant references.
Refs: main_implementation plan.md P05.3-P05.5, P06.1; api design.md §§5.1, 6.2; Phase: P05/P06.
"""

from alembic import op

revision = "0025_run_evidence"
down_revision = "0024_run_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE research.run_stage_checkpoints (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,run_id uuid NOT NULL,
      stage varchar(64) NOT NULL,generation bigint NOT NULL CHECK(generation>0),
      input_digest text NOT NULL CHECK(input_digest ~ '^sha256:[0-9a-f]{64}$'),
      output_digest text NOT NULL CHECK(output_digest ~ '^sha256:[0-9a-f]{64}$'),
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,project_id,run_id,stage),
      FOREIGN KEY(tenant_id,project_id,run_id)
        REFERENCES research.compute_runs(tenant_id,project_id,id))""")
    op.execute("""CREATE TABLE research.run_artifacts (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,
      investigation_id uuid NOT NULL,run_id uuid NOT NULL,evidence_manifest_id uuid NOT NULL,
      scientific_status varchar(32) NOT NULL CHECK(scientific_status IN ('supported','insufficient_evidence')),
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,project_id,id),UNIQUE(tenant_id,project_id,run_id),
      FOREIGN KEY(tenant_id,project_id,investigation_id)
        REFERENCES research.investigations(tenant_id,project_id,id),
      FOREIGN KEY(tenant_id,project_id,run_id)
        REFERENCES research.compute_runs(tenant_id,project_id,id),
      FOREIGN KEY(tenant_id,project_id,evidence_manifest_id)
        REFERENCES research.science_evidence_manifests(tenant_id,project_id,id))""")
    for table in ("run_stage_checkpoints", "run_artifacts"):
        op.execute(f"ALTER TABLE research.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE research.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY {table}_tenant ON research.{table}
          USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
          WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
        op.execute(f"GRANT SELECT,INSERT ON research.{table} TO orca_runtime")
        op.execute(f"""CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON research.{table}
          FOR EACH ROW EXECUTE FUNCTION research.protect_tabular_evidence()""")
    op.execute("""CREATE INDEX run_artifacts_investigation
      ON research.run_artifacts(tenant_id,project_id,investigation_id,created_at)""")
    op.execute("""CREATE FUNCTION iam.resolve_artifact_tenant(p_subject text,p_artifact uuid)
      RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,iam,research AS $$
      SELECT a.tenant_id FROM research.run_artifacts a
      WHERE a.id=p_artifact AND iam.resolve_investigation_tenant(p_subject,a.investigation_id)=a.tenant_id
      LIMIT 1 $$""")
    op.execute("REVOKE ALL ON FUNCTION iam.resolve_artifact_tenant(text,uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION iam.resolve_artifact_tenant(text,uuid) TO orca_runtime")


def downgrade() -> None:
    raise RuntimeError("Run evidence history requires explicit retention review")
