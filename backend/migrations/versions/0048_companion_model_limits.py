"""Purpose: persist per-organization and per-user companion model admission counters.
Behavior: isolate counters by tenant and roll each counter over on the database clock.
Refs: main implementation plan P12.2; A-API §§14,17.
"""

from alembic import op

revision = "0048_companion_model_limits"
down_revision = "0047_companion_replies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE research.companion_model_counters (
      tenant_id uuid NOT NULL REFERENCES iam.organizations(tenant_id),
      issuer text NOT NULL, subject text NOT NULL,
      window_start timestamptz NOT NULL,
      used integer NOT NULL CHECK (used > 0),
      CHECK ((issuer='')=(subject='')),
      PRIMARY KEY (tenant_id,issuer,subject))""")
    op.execute("ALTER TABLE research.companion_model_counters ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE research.companion_model_counters FORCE ROW LEVEL SECURITY")
    scope = """tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid
        AND ((issuer='' AND subject='') OR
             (issuer=current_setting('orca.companion_issuer',true)
              AND subject=current_setting('orca.companion_subject',true)))"""
    op.execute(f"""CREATE POLICY companion_counter_scope ON research.companion_model_counters
      TO orca_runtime USING ({scope}) WITH CHECK ({scope})""")
    op.execute("GRANT SELECT,INSERT ON research.companion_model_counters TO orca_runtime")
    op.execute("""GRANT UPDATE(window_start,used)
      ON research.companion_model_counters TO orca_runtime""")


def downgrade() -> None:
    raise RuntimeError("Companion model counters require explicit recovery review")
