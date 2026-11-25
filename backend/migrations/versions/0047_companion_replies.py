"""Purpose: persist cited companion replies without storing another copy of the question.
Behavior: inherit conversation owner isolation and keep saved replies immutable.
Refs: main implementation plan P12.1–P12.2; A-API §3.13.
"""

from alembic import op

revision = "0047_companion_replies"
down_revision = "0046_companion_preferences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE research.companion_replies (
      conversation_id uuid NOT NULL REFERENCES research.companion_conversations(id),
      id uuid NOT NULL,
      artifact_id uuid NOT NULL,
      manifest_digest text NOT NULL CHECK(manifest_digest ~ '^sha256:[0-9a-f]{64}$'),
      body text NOT NULL CHECK(length(body) BETWEEN 1 AND 2000),
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      PRIMARY KEY(conversation_id,id))""")
    op.execute("ALTER TABLE research.companion_replies ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE research.companion_replies FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY companion_parent_owner ON research.companion_replies TO orca_runtime
      USING (EXISTS (SELECT 1 FROM research.companion_conversations parent
        WHERE parent.id=companion_replies.conversation_id))
      WITH CHECK (EXISTS (SELECT 1 FROM research.companion_conversations parent
        WHERE parent.id=companion_replies.conversation_id))""")
    op.execute("GRANT SELECT, INSERT ON research.companion_replies TO orca_runtime")


def downgrade() -> None:
    raise RuntimeError("Companion replies require explicit recovery review")
