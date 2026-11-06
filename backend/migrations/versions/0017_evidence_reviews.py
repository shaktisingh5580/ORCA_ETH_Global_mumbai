"""Purpose: preserve candidate reviews and correction lineage without replacing original results.
Behavior: add tenant RLS, lease state and append-only candidate references.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A03; Phase: A03.
"""

from alembic import op

revision = "0017_evidence_reviews"
down_revision = "0016_reasoning_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE FUNCTION research.protect_reasoning_result() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN IF OLD.state='completed' THEN RAISE EXCEPTION 'approved reasoning results are immutable';
      END IF; RETURN NEW; END $$""")
    op.execute("""CREATE TRIGGER reasoning_result_immutable BEFORE UPDATE OR DELETE ON research.reasoning_runs
      FOR EACH ROW EXECUTE FUNCTION research.protect_reasoning_result()""")
    op.execute("""CREATE TABLE research.evidence_reviews (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,run_id uuid NOT NULL,
      created_by text NOT NULL,candidate jsonb NOT NULL,candidate_digest text NOT NULL,parent_review_id uuid,
      state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','reviewing','completed','failed')),
      findings jsonb,decision jsonb,error_code text,lease_token uuid,lease_until timestamptz,
      attempts integer NOT NULL DEFAULT 0,created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,project_id,id),UNIQUE(tenant_id,project_id,run_id,candidate_digest),
      FOREIGN KEY(tenant_id,project_id,run_id) REFERENCES research.reasoning_runs(tenant_id,project_id,id),
      FOREIGN KEY(tenant_id,project_id,parent_review_id) REFERENCES research.evidence_reviews(tenant_id,project_id,id))""")
    op.execute("ALTER TABLE research.evidence_reviews ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE research.evidence_reviews FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY evidence_reviews_tenant ON research.evidence_reviews
      USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
      WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT,INSERT,UPDATE ON research.evidence_reviews TO orca_runtime")
    op.execute("""CREATE FUNCTION research.protect_completed_review() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN IF OLD.state='completed' OR NEW.candidate IS DISTINCT FROM OLD.candidate THEN
      RAISE EXCEPTION 'review evidence is immutable'; END IF; RETURN NEW; END $$""")
    op.execute("""CREATE TRIGGER review_immutable BEFORE UPDATE ON research.evidence_reviews
      FOR EACH ROW EXECUTE FUNCTION research.protect_completed_review()""")
    op.execute("""CREATE FUNCTION iam.pending_evidence_reviews() RETURNS TABLE(tenant_id uuid,review_id uuid)
      LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,research AS $$
      SELECT tenant_id,id FROM research.evidence_reviews WHERE state='queued'
      OR (state='reviewing' AND lease_until<clock_timestamp()) ORDER BY created_at LIMIT 10 $$""")
    op.execute("REVOKE ALL ON FUNCTION iam.pending_evidence_reviews() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION iam.pending_evidence_reviews() TO orca_runtime")


def downgrade() -> None:
    raise RuntimeError("Review history removal requires retention approval")
