"""Purpose: preserve exact-digest, append-only investigation plan approvals.
Behavior: bind each approval to one immutable plan with tenant RLS and no update/delete grant.
Refs: main_implementation plan.md P05.1; api design.md §§3.6, 5.1; Phase: P05.
"""

from alembic import op

revision = "0022_plan_approvals"
down_revision = "0021_investigations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE research.investigation_plans
      ADD CONSTRAINT investigation_plans_scoped_plan_id_unique
      UNIQUE(tenant_id,project_id,investigation_id,id)""")
    op.execute("""CREATE TABLE research.plan_approvals (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,
      investigation_id uuid NOT NULL,plan_id uuid NOT NULL,
      plan_digest text NOT NULL CHECK(plan_digest ~ '^sha256:[0-9a-f]{64}$'),
      accepted_assumptions jsonb NOT NULL CHECK(jsonb_typeof(accepted_assumptions)='array'),
      approver_comment text,approved_by varchar(255) NOT NULL,idempotency_key varchar(128) NOT NULL,
      approved_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,project_id,id),UNIQUE(tenant_id,project_id,plan_id),
      UNIQUE(tenant_id,project_id,investigation_id,idempotency_key),
      FOREIGN KEY(tenant_id,project_id,investigation_id)
        REFERENCES research.investigations(tenant_id,project_id,id) ON DELETE RESTRICT,
      FOREIGN KEY(tenant_id,project_id,investigation_id,plan_id)
        REFERENCES research.investigation_plans(tenant_id,project_id,investigation_id,id)
        ON DELETE RESTRICT)""")
    op.execute("ALTER TABLE research.plan_approvals ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE research.plan_approvals FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY plan_approvals_tenant ON research.plan_approvals
      USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
      WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT,INSERT ON research.plan_approvals TO orca_runtime")
    op.execute("""CREATE FUNCTION research.validate_plan_approval() RETURNS trigger
      LANGUAGE plpgsql AS $$ DECLARE persisted_digest text;
      BEGIN
        SELECT plan_digest INTO persisted_digest FROM research.investigation_plans
        WHERE tenant_id=NEW.tenant_id AND project_id=NEW.project_id
          AND investigation_id=NEW.investigation_id AND id=NEW.plan_id;
        IF persisted_digest IS NULL OR persisted_digest <> NEW.plan_digest THEN
          RAISE EXCEPTION 'plan approval digest does not match immutable plan';
        END IF;
        RETURN NEW;
      END $$""")
    op.execute("""CREATE TRIGGER plan_approvals_digest_bound
      BEFORE INSERT ON research.plan_approvals
      FOR EACH ROW EXECUTE FUNCTION research.validate_plan_approval()""")
    op.execute("""CREATE FUNCTION research.protect_plan_approval() RETURNS trigger
      LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'plan approvals are immutable'; END $$""")
    op.execute("""CREATE TRIGGER plan_approvals_immutable
      BEFORE UPDATE OR DELETE ON research.plan_approvals
      FOR EACH ROW EXECUTE FUNCTION research.protect_plan_approval()""")
    op.execute("""CREATE INDEX plan_approvals_by_investigation
      ON research.plan_approvals(tenant_id,project_id,investigation_id,approved_at)""")


def downgrade() -> None:
    raise RuntimeError("Plan approval history requires explicit retention review")
