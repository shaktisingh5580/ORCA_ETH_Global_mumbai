"""Purpose: persist immutable project-scoped tabular evidence and approved transformations.
Behavior: additive RLS-protected tables preserve original inputs and mapping/result lineage.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md sections 4,7; Phase: A01.
"""

from alembic import op

revision = "0015_tabular_evidence"
down_revision = "0014_agent_coordination"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE research.tabular_datasets (
      id uuid PRIMARY KEY, tenant_id uuid NOT NULL, project_id uuid NOT NULL,
      created_by text NOT NULL, name text NOT NULL, format text NOT NULL CHECK(format IN ('csv','json')),
      source_mode text NOT NULL CHECK(source_mode IN ('uploaded','synthetic_demo')),
      content text NOT NULL CHECK(octet_length(content)<=5242880), content_digest text NOT NULL,
      inspection jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,project_id,id), UNIQUE(tenant_id,project_id,content_digest,source_mode),
      FOREIGN KEY(tenant_id,project_id) REFERENCES research.projects(tenant_id,id))""")
    op.execute("""CREATE TABLE research.tabular_normalizations (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,dataset_id uuid NOT NULL,
      approved_by text NOT NULL,mapping jsonb NOT NULL,mapping_digest text NOT NULL,
      result jsonb NOT NULL,artifact_digest text NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,project_id,id),UNIQUE(tenant_id,dataset_id,mapping_digest),
      FOREIGN KEY(tenant_id,project_id,dataset_id) REFERENCES research.tabular_datasets(tenant_id,project_id,id))""")
    op.execute("""CREATE FUNCTION research.protect_tabular_evidence() RETURNS trigger
      LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'tabular evidence is immutable'; END $$""")
    for table in ("tabular_datasets", "tabular_normalizations"):
        op.execute(f"ALTER TABLE research.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE research.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY {table}_tenant ON research.{table}
          USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
          WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
        op.execute(f"GRANT SELECT,INSERT ON research.{table} TO orca_runtime")
        op.execute(f"""CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON research.{table}
          FOR EACH ROW EXECUTE FUNCTION research.protect_tabular_evidence()""")


def downgrade() -> None:
    raise RuntimeError("Removing evidence requires explicit retention review")
