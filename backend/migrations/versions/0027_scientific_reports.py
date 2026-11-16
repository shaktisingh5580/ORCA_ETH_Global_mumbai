"""Purpose: persist immutable P06 scientific report revisions and protected renderings.
Behavior: bind every format to one verified run artifact and permit only one draft-to-published transition.
Refs: main_implementation plan.md P06.2/P06.4; api design.md §3.7; Phase: P06.
"""

from alembic import op

revision = "0027_scientific_reports"
down_revision = "0026_scientific_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE research.scientific_reports (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,
      investigation_id uuid NOT NULL,artifact_id uuid NOT NULL,
      revision integer NOT NULL CHECK(revision>0),created_by varchar(255) NOT NULL,
      document jsonb NOT NULL CHECK(jsonb_typeof(document)='object'),
      document_digest text NOT NULL CHECK(document_digest ~ '^sha256:[0-9a-f]{64}$'),
      html_bytes bytea NOT NULL,html_digest text NOT NULL CHECK(html_digest ~ '^sha256:[0-9a-f]{64}$'),
      pdf_bytes bytea NOT NULL,pdf_digest text NOT NULL CHECK(pdf_digest ~ '^sha256:[0-9a-f]{64}$'),
      status varchar(16) NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published')),
      created_at timestamptz NOT NULL DEFAULT now(),published_at timestamptz,
      UNIQUE(tenant_id,project_id,id),UNIQUE(tenant_id,project_id,investigation_id,revision),
      FOREIGN KEY(tenant_id,project_id,investigation_id)
        REFERENCES research.investigations(tenant_id,project_id,id),
      FOREIGN KEY(tenant_id,project_id,artifact_id)
        REFERENCES research.run_artifacts(tenant_id,project_id,id))""")
    op.execute("ALTER TABLE research.scientific_reports ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE research.scientific_reports FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY scientific_reports_tenant ON research.scientific_reports
      USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
      WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
    op.execute(
        "GRANT SELECT,INSERT,UPDATE(status,published_at) ON research.scientific_reports TO orca_runtime"
    )
    op.execute("""CREATE FUNCTION research.protect_report_publication() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP='DELETE' THEN
          RAISE EXCEPTION 'report revisions cannot be deleted';
        END IF;
        IF OLD.status<>'draft' OR NEW.status<>'published' OR NEW.published_at IS NULL THEN
          RAISE EXCEPTION 'report revisions are immutable after publication';
        END IF;
        RETURN NEW;
      END $$""")
    op.execute("""CREATE TRIGGER scientific_report_publication BEFORE UPDATE OR DELETE
      ON research.scientific_reports FOR EACH ROW EXECUTE FUNCTION research.protect_report_publication()""")
    op.execute("""CREATE INDEX scientific_reports_investigation
      ON research.scientific_reports(tenant_id,project_id,investigation_id,revision DESC)""")
    op.execute("""CREATE FUNCTION iam.resolve_report_tenant(p_subject text,p_report uuid)
      RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,iam,research AS $$
      SELECT r.tenant_id FROM research.scientific_reports r
      WHERE r.id=p_report AND iam.resolve_artifact_tenant(p_subject,r.artifact_id)=r.tenant_id
      LIMIT 1 $$""")
    op.execute("REVOKE ALL ON FUNCTION iam.resolve_report_tenant(text,uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION iam.resolve_report_tenant(text,uuid) TO orca_runtime")


def downgrade() -> None:
    raise RuntimeError("Published scientific reports require explicit retention review")
