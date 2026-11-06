"""Purpose: queue consented research evidence publications without changing existing A2A tables.
Behavior: retain immutable evidence digests, durable receipts and tenant isolation.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A04; Phase: A04 evidence publication increment.
"""

from alembic import op

revision = "0018_research_anchors"
down_revision = "0017_evidence_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE research.evidence_anchors (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,run_id uuid NOT NULL,
      created_by text NOT NULL,manifest jsonb NOT NULL,manifest_digest text NOT NULL,
      parent_key text NOT NULL DEFAULT '',state text NOT NULL DEFAULT 'queued'
      CHECK(state IN ('queued','publishing','indexed','failed','reconciliation_required')),
      envelope jsonb,receipt jsonb,error_code text,lease_token uuid,lease_until timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,project_id,run_id,manifest_digest),
      FOREIGN KEY(tenant_id,project_id,run_id) REFERENCES research.reasoning_runs(tenant_id,project_id,id))""")
    op.execute("ALTER TABLE research.evidence_anchors ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE research.evidence_anchors FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY evidence_anchors_tenant ON research.evidence_anchors
      USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
      WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT,INSERT,UPDATE ON research.evidence_anchors TO orca_runtime")
    op.execute("""CREATE FUNCTION research.protect_evidence_anchor() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN IF OLD.state='indexed' OR NEW.manifest IS DISTINCT FROM OLD.manifest
      OR NEW.manifest_digest IS DISTINCT FROM OLD.manifest_digest OR NEW.parent_key IS DISTINCT FROM OLD.parent_key
      OR (OLD.envelope IS NOT NULL AND NEW.envelope IS DISTINCT FROM OLD.envelope)
      THEN RAISE EXCEPTION 'published evidence is immutable'; END IF; RETURN NEW; END $$""")
    op.execute("""CREATE TRIGGER evidence_anchor_immutable BEFORE UPDATE ON research.evidence_anchors
      FOR EACH ROW EXECUTE FUNCTION research.protect_evidence_anchor()""")
    op.execute("""CREATE FUNCTION iam.pending_evidence_anchors() RETURNS TABLE(tenant_id uuid,anchor_id uuid)
      LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,research AS $$
      SELECT tenant_id,id FROM research.evidence_anchors WHERE state='queued'
      OR (state='publishing' AND lease_until<clock_timestamp()) ORDER BY created_at LIMIT 10 $$""")
    op.execute("REVOKE ALL ON FUNCTION iam.pending_evidence_anchors() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION iam.pending_evidence_anchors() TO orca_runtime")


def downgrade() -> None:
    raise RuntimeError("Published evidence requires retention review")
