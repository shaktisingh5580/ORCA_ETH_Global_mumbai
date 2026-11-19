"""Purpose: persist P10 one-time agent identity binding challenges and signature evidence.
Behavior: fence submissions by agent binding, actor and expiry under tenant RLS.
Refs: main_implementation plan.md P10.3; api design.md §§3.11,4.6; Phase: P10.
"""

from alembic import op

revision = "0043_identity_binding_challenges"
down_revision = "0042_remote_dispatch_fallback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE federation.identity_binding_challenges(
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,organization_id uuid NOT NULL,
      agent_id uuid NOT NULL,binding_version uuid NOT NULL,agent_version integer NOT NULL,
      actor text NOT NULL, manifest jsonb NOT NULL,expires_at timestamptz NOT NULL,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      consumed_at timestamptz,evidence jsonb,
      FOREIGN KEY(tenant_id,organization_id,agent_id)
        REFERENCES federation.registrations(tenant_id,organization_id,id),
      FOREIGN KEY(tenant_id,agent_id,binding_version)
        REFERENCES federation.registration_versions(tenant_id,agent_id,id),
      CHECK((consumed_at IS NULL)=(evidence IS NULL)))""")
    op.execute("ALTER TABLE federation.identity_binding_challenges ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE federation.identity_binding_challenges FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY identity_binding_challenges_tenant
      ON federation.identity_binding_challenges
      USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
      WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT,INSERT ON federation.identity_binding_challenges TO orca_runtime")
    op.execute("""GRANT UPDATE(consumed_at,evidence)
      ON federation.identity_binding_challenges TO orca_runtime""")
    op.execute("""CREATE INDEX identity_binding_unconsumed
      ON federation.identity_binding_challenges(tenant_id,agent_id,actor,expires_at)
      WHERE consumed_at IS NULL""")
    op.execute("""CREATE FUNCTION federation.consume_binding_challenge_once()
      RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
      BEGIN
        IF OLD.consumed_at IS NOT NULL OR OLD.expires_at<=clock_timestamp()
          OR NEW.consumed_at IS NULL OR NEW.evidence IS NULL THEN
          RAISE EXCEPTION 'identity binding challenge cannot be consumed' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
      END $$""")
    op.execute("""CREATE TRIGGER consume_binding_challenge_once
      BEFORE UPDATE ON federation.identity_binding_challenges
      FOR EACH ROW EXECUTE FUNCTION federation.consume_binding_challenge_once()""")


def downgrade() -> None:
    raise RuntimeError("Identity binding evidence requires explicit recovery review")
