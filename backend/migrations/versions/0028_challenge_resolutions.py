"""Purpose: preserve P07 scientific challenge adjudications without changing original evidence.
Behavior: allow one append-only resolution per exact challenge under tenant RLS.
Refs: main_implementation plan.md P07.2; api design.md §§3.8, 4.5; Phase: P07.
"""

from alembic import op

revision = "0028_challenge_resolutions"
down_revision = "0027_scientific_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE research.challenge_resolutions (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,
      challenge_id uuid NOT NULL,resolver_subject varchar(255) NOT NULL,
      verdict varchar(16) NOT NULL CHECK(verdict IN ('upheld','rejected','inconclusive')),
      arbitration_refs jsonb NOT NULL CHECK(jsonb_typeof(arbitration_refs)='array'),
      limitations text NOT NULL CHECK(length(limitations) BETWEEN 10 AND 4000),
      independence_verified boolean NOT NULL DEFAULT false,
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,project_id,id),UNIQUE(tenant_id,project_id,challenge_id),
      FOREIGN KEY(tenant_id,project_id,challenge_id)
        REFERENCES research.scientific_challenges(tenant_id,project_id,id))""")
    op.execute("ALTER TABLE research.challenge_resolutions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE research.challenge_resolutions FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY challenge_resolutions_tenant ON research.challenge_resolutions
      USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
      WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT,INSERT ON research.challenge_resolutions TO orca_runtime")
    op.execute("""CREATE TRIGGER challenge_resolutions_immutable
      BEFORE UPDATE OR DELETE ON research.challenge_resolutions
      FOR EACH ROW EXECUTE FUNCTION research.protect_tabular_evidence()""")
    op.execute("""CREATE OR REPLACE FUNCTION iam.resolve_challenge_tenant(p_subject text,p_challenge uuid)
      RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,iam,research AS $$
      SELECT c.tenant_id FROM research.scientific_challenges c
      WHERE c.id=p_challenge AND (
        iam.resolve_artifact_tenant(p_subject,c.artifact_id)=c.tenant_id OR
        EXISTS(SELECT 1 FROM research.artifact_review_grants g
          WHERE g.tenant_id=c.tenant_id AND g.project_id=c.project_id
          AND g.artifact_id=c.artifact_id AND g.reviewer_subject=p_subject
          AND g.revoked_at IS NULL AND g.expires_at>clock_timestamp()))
      LIMIT 1 $$""")


def downgrade() -> None:
    raise RuntimeError("Scientific challenge resolutions require explicit retention review")
