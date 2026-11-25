"""Purpose: durably queue private companion reply requests.
Behavior: enforce conversation RLS and keep only short-lived work input plus a terminal receipt.
Refs: main implementation plan P12.2; A-API §§3.13,17.
"""

from alembic import op

revision = "0049_companion_reply_jobs"
down_revision = "0048_companion_model_limits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE research.companion_reply_jobs (
      conversation_id uuid NOT NULL REFERENCES research.companion_conversations(id),
      id uuid NOT NULL,
      input_digest text NOT NULL CHECK(input_digest ~ '^[0-9a-f]{64}$'),
      input jsonb, state text NOT NULL DEFAULT 'queued'
        CHECK(state IN ('queued','running','completed','expired','interrupted','rate_limited')),
      result jsonb, lease uuid, lease_until timestamptz,
      expires_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '10 minutes',
      PRIMARY KEY(conversation_id,id),
      CHECK ((state='running')=(lease IS NOT NULL)),
      CHECK ((lease IS NULL)=(lease_until IS NULL)),
      CHECK ((state IN ('queued','running'))=(input IS NOT NULL)),
      CHECK ((state='completed')=(result IS NOT NULL)))""")
    op.execute("ALTER TABLE research.companion_reply_jobs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE research.companion_reply_jobs FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY companion_parent_owner ON research.companion_reply_jobs TO orca_runtime
      USING (EXISTS (SELECT 1 FROM research.companion_conversations parent
        WHERE parent.id=companion_reply_jobs.conversation_id))
      WITH CHECK (EXISTS (SELECT 1 FROM research.companion_conversations parent
        WHERE parent.id=companion_reply_jobs.conversation_id))""")
    op.execute("GRANT SELECT,INSERT ON research.companion_reply_jobs TO orca_runtime")
    op.execute("""GRANT UPDATE(input,state,result,lease,lease_until)
      ON research.companion_reply_jobs TO orca_runtime""")


def downgrade() -> None:
    raise RuntimeError("Companion reply jobs require explicit recovery review")
