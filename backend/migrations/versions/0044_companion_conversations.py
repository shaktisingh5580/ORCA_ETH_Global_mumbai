"""Purpose: persist private companion sessions independently of research evidence.
Behavior: enforce tenant, token issuer and owner visibility under runtime RLS.
Refs: main implementation plan P12.1; A-API §3.13; W-MEM6 §6.
"""

from alembic import op

revision = "0044_companion_conversations"
down_revision = "0043_identity_binding_challenges"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE research.companion_conversations(
      id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES iam.organizations(tenant_id),
      owner_subject text NOT NULL CHECK(length(owner_subject) BETWEEN 1 AND 512),
      owner_issuer text NOT NULL CHECK(length(owner_issuer) BETWEEN 1 AND 512),
      title text NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 160),
      locale text NOT NULL CHECK(locale IN ('en')),
      archived boolean NOT NULL DEFAULT false,
      version integer NOT NULL DEFAULT 1 CHECK(version>0),
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      updated_at timestamptz NOT NULL DEFAULT clock_timestamp())""")
    op.execute("""CREATE INDEX companion_owner_cursor
      ON research.companion_conversations(tenant_id,owner_issuer,owner_subject,id)""")
    op.execute("ALTER TABLE research.companion_conversations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE research.companion_conversations FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY companion_owner ON research.companion_conversations
      TO orca_runtime
      USING (tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid
        AND owner_subject=nullif(current_setting('orca.companion_subject',true),'')
        AND owner_issuer=nullif(current_setting('orca.companion_issuer',true),''))
      WITH CHECK (tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid
        AND owner_subject=nullif(current_setting('orca.companion_subject',true),'')
        AND owner_issuer=nullif(current_setting('orca.companion_issuer',true),''))""")
    op.execute("GRANT SELECT, INSERT ON research.companion_conversations TO orca_runtime")
    op.execute("""GRANT UPDATE(title,locale,archived,version,updated_at)
      ON research.companion_conversations TO orca_runtime""")


def downgrade() -> None:
    raise RuntimeError("Companion conversations require explicit recovery review")
