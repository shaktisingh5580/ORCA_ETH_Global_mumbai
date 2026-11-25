"""Purpose: store private companion preferences and at most one consented location.
Behavior: enforce owner RLS and require consent for any persisted coordinates.
Refs: main implementation plan P12.1; A-API §3.13; W-MEM6 §§24–25,41.
"""

from alembic import op

revision = "0046_companion_preferences"
down_revision = "0045_companion_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE research.companion_preferences(
      tenant_id uuid NOT NULL REFERENCES iam.organizations(tenant_id),
      owner_subject text NOT NULL CHECK(length(owner_subject) BETWEEN 1 AND 512),
      owner_issuer text NOT NULL CHECK(length(owner_issuer) BETWEEN 1 AND 512),
      units text NOT NULL DEFAULT 'metric' CHECK(units IN ('metric','nautical')),
      location_consent boolean NOT NULL DEFAULT false,
      location_id uuid,
      latitude double precision CHECK(latitude BETWEEN -90 AND 90),
      longitude double precision CHECK(longitude BETWEEN -180 AND 180),
      version integer NOT NULL DEFAULT 1 CHECK(version>0),
      PRIMARY KEY(tenant_id,owner_subject,owner_issuer),
      CHECK ((location_id IS NOT NULL)=(latitude IS NOT NULL)
        AND (location_id IS NOT NULL)=(longitude IS NOT NULL)
        AND (location_id IS NULL OR location_consent)))""")
    op.execute("ALTER TABLE research.companion_preferences ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE research.companion_preferences FORCE ROW LEVEL SECURITY")
    scope = """tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid
        AND owner_subject=nullif(current_setting('orca.companion_subject',true),'')
        AND owner_issuer=nullif(current_setting('orca.companion_issuer',true),'')"""
    op.execute(f"""CREATE POLICY companion_preferences_owner
      ON research.companion_preferences TO orca_runtime USING ({scope}) WITH CHECK ({scope})""")
    op.execute("GRANT SELECT,INSERT ON research.companion_preferences TO orca_runtime")
    op.execute("""GRANT UPDATE(units,location_consent,location_id,latitude,longitude,version)
      ON research.companion_preferences TO orca_runtime""")


def downgrade() -> None:
    raise RuntimeError("Companion preferences require explicit recovery review")
