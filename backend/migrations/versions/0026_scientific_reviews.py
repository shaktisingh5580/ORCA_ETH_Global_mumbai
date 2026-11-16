"""Purpose: persist P07 scoped reviewer grants and append-only scientific disagreements.
Behavior: preserve the original artifact while granting bounded external review and immutable challenge records.
Refs: main_implementation plan.md P07.2; api design.md §§3.8, 4.5; Phase: P07.
"""

from alembic import op

revision = "0026_scientific_reviews"
down_revision = "0025_run_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE research.artifact_review_grants (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,artifact_id uuid NOT NULL,
      reviewer_subject varchar(255) NOT NULL,granted_by varchar(255) NOT NULL,
      expires_at timestamptz NOT NULL,revoked_at timestamptz,created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,project_id,artifact_id,reviewer_subject),
      FOREIGN KEY(tenant_id,project_id,artifact_id) REFERENCES research.run_artifacts(tenant_id,project_id,id))""")
    op.execute("""CREATE TABLE research.scientific_challenges (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,artifact_id uuid NOT NULL,
      reviewer_subject varchar(255) NOT NULL,discrepancy_type varchar(40) NOT NULL
        CHECK(discrepancy_type IN ('wrong_baseline','numerical_mismatch','method_mismatch','missing_source','unsupported_claim')),
      source_refs jsonb NOT NULL CHECK(jsonb_typeof(source_refs)='array'),
      statement text NOT NULL CHECK(length(statement) BETWEEN 10 AND 4000),
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,project_id,id),
      FOREIGN KEY(tenant_id,project_id,artifact_id) REFERENCES research.run_artifacts(tenant_id,project_id,id))""")
    op.execute("""CREATE TABLE research.challenge_responses (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,challenge_id uuid NOT NULL,
      responder_subject varchar(255) NOT NULL,response_artifact_id uuid,
      statement text NOT NULL CHECK(length(statement) BETWEEN 10 AND 4000),
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,project_id,id),
      FOREIGN KEY(tenant_id,project_id,challenge_id)
        REFERENCES research.scientific_challenges(tenant_id,project_id,id),
      FOREIGN KEY(tenant_id,project_id,response_artifact_id)
        REFERENCES research.run_artifacts(tenant_id,project_id,id))""")
    op.execute("""CREATE TABLE research.independent_reviews (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,artifact_id uuid NOT NULL,
      reviewer_subject varchar(255) NOT NULL,verdict varchar(24) NOT NULL
        CHECK(verdict IN ('agrees','disagrees','inconclusive')),
      statement text NOT NULL CHECK(length(statement) BETWEEN 10 AND 4000),
      reproduction_digest text CHECK(reproduction_digest IS NULL OR reproduction_digest ~ '^sha256:[0-9a-f]{64}$'),
      independence_verified boolean NOT NULL DEFAULT false,
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,project_id,id),
      FOREIGN KEY(tenant_id,project_id,artifact_id) REFERENCES research.run_artifacts(tenant_id,project_id,id))""")
    for table in (
        "artifact_review_grants",
        "scientific_challenges",
        "challenge_responses",
        "independent_reviews",
    ):
        op.execute(f"ALTER TABLE research.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE research.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY {table}_tenant ON research.{table}
          USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
          WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
        op.execute(f"GRANT SELECT,INSERT ON research.{table} TO orca_runtime")
    op.execute("GRANT UPDATE(revoked_at) ON research.artifact_review_grants TO orca_runtime")
    op.execute("""CREATE FUNCTION research.protect_review_grant() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL OR NEW.revoked_at<OLD.created_at THEN
          RAISE EXCEPTION 'review grant can only be revoked once';
        END IF;
        RETURN NEW;
      END $$""")
    op.execute("""CREATE TRIGGER artifact_review_grant_revocation
      BEFORE UPDATE ON research.artifact_review_grants
      FOR EACH ROW EXECUTE FUNCTION research.protect_review_grant()""")
    for table in ("scientific_challenges", "challenge_responses", "independent_reviews"):
        op.execute(f"""CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON research.{table}
          FOR EACH ROW EXECUTE FUNCTION research.protect_tabular_evidence()""")
    op.execute("""CREATE INDEX scientific_challenges_artifact
      ON research.scientific_challenges(tenant_id,project_id,artifact_id,created_at)""")
    op.execute("""CREATE INDEX artifact_review_grants_active
      ON research.artifact_review_grants(tenant_id,project_id,artifact_id,reviewer_subject,expires_at)
      WHERE revoked_at IS NULL""")
    op.execute("""CREATE INDEX independent_reviews_artifact
      ON research.independent_reviews(tenant_id,project_id,artifact_id,created_at)""")
    op.execute("""CREATE FUNCTION iam.resolve_review_artifact_tenant(p_subject text,p_artifact uuid)
      RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,iam,research AS $$
      SELECT a.tenant_id FROM research.run_artifacts a
      WHERE a.id=p_artifact AND (
        iam.resolve_artifact_tenant(p_subject,p_artifact)=a.tenant_id OR
        EXISTS(SELECT 1 FROM research.artifact_review_grants g
          WHERE g.tenant_id=a.tenant_id AND g.project_id=a.project_id AND g.artifact_id=a.id
          AND g.reviewer_subject=p_subject AND g.revoked_at IS NULL AND g.expires_at>clock_timestamp()))
      LIMIT 1 $$""")
    op.execute("REVOKE ALL ON FUNCTION iam.resolve_review_artifact_tenant(text,uuid) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION iam.resolve_review_artifact_tenant(text,uuid) TO orca_runtime"
    )
    op.execute("""CREATE FUNCTION iam.resolve_challenge_tenant(p_subject text,p_challenge uuid)
      RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,iam,research AS $$
      SELECT c.tenant_id FROM research.scientific_challenges c
      WHERE c.id=p_challenge AND iam.resolve_artifact_tenant(p_subject,c.artifact_id)=c.tenant_id
      LIMIT 1 $$""")
    op.execute("REVOKE ALL ON FUNCTION iam.resolve_challenge_tenant(text,uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION iam.resolve_challenge_tenant(text,uuid) TO orca_runtime")


def downgrade() -> None:
    raise RuntimeError("Independent review history requires explicit retention review")
