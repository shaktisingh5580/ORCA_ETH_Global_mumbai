"""Purpose: persist private user message history and resumable intake checkpoints.
Behavior: scope both child tables to the parent conversation's owner and tenant.
Refs: main implementation plan P12.1; A-API §3.13; W-MEM6 §§4.1,17.
"""

from alembic import op

revision = "0045_companion_history"
down_revision = "0044_companion_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE research.companion_messages(
      conversation_id uuid NOT NULL REFERENCES research.companion_conversations(id),
      sequence bigint NOT NULL CHECK(sequence>0), request_id uuid NOT NULL,
      content text NOT NULL CHECK(length(content) BETWEEN 1 AND 8000),
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      PRIMARY KEY(conversation_id,sequence), UNIQUE(conversation_id,request_id))""")
    op.execute("""CREATE TABLE research.companion_checkpoints(
      conversation_id uuid PRIMARY KEY, message_sequence bigint NOT NULL,
      updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      expires_at timestamptz NOT NULL,
      CHECK (expires_at > updated_at AND expires_at <= updated_at + interval '24 hours'),
      FOREIGN KEY(conversation_id,message_sequence)
        REFERENCES research.companion_messages(conversation_id,sequence))""")
    for table in ("companion_messages", "companion_checkpoints"):
        op.execute(f"ALTER TABLE research.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE research.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY companion_parent_owner ON research.{table} TO orca_runtime
          USING (EXISTS (SELECT 1 FROM research.companion_conversations parent
                        WHERE parent.id={table}.conversation_id))
          WITH CHECK (EXISTS (SELECT 1 FROM research.companion_conversations parent
                             WHERE parent.id={table}.conversation_id))""")
        op.execute(f"GRANT SELECT,INSERT ON research.{table} TO orca_runtime")
    op.execute("""GRANT UPDATE(message_sequence,updated_at,expires_at), DELETE
      ON research.companion_checkpoints TO orca_runtime""")


def downgrade() -> None:
    raise RuntimeError("Companion message history requires explicit recovery review")
