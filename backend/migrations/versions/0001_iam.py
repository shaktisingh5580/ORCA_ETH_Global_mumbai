"""Purpose: establish PostGIS, IAM tenants and non-owner runtime role.
Behavior: create ordered identity tables and deny runtime ownership/BYPASSRLS.
Refs: main plan P02.1/P02.3; docs/04_database.md sections 2,9; Phase: P02.
"""

from alembic import op

revision = "0001_iam"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS extensions")
    op.execute("""DO $$ DECLARE installed_schema text; BEGIN
      SELECT n.nspname INTO installed_schema FROM pg_extension e
      JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='postgis';
      IF installed_schema IS NULL THEN
        EXECUTE 'CREATE EXTENSION postgis SCHEMA extensions';
      ELSIF installed_schema <> 'extensions' THEN
        RAISE EXCEPTION 'PostGIS must be installed in extensions schema, found %', installed_schema;
      END IF;
    END $$""")
    op.execute("CREATE SCHEMA IF NOT EXISTS iam")
    op.execute("""DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'orca_runtime') THEN
        RAISE EXCEPTION 'orca_runtime role must be provisioned before migration';
      END IF;
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'orca_runtime' AND (rolsuper OR rolbypassrls)) THEN
        RAISE EXCEPTION 'orca_runtime must not have SUPERUSER or BYPASSRLS';
      END IF;
    END $$""")
    op.execute("""CREATE TABLE iam.organizations (
      id uuid PRIMARY KEY, tenant_id uuid NOT NULL UNIQUE, name varchar(200) NOT NULL,
      slug varchar(63) NOT NULL UNIQUE, visibility varchar(24) NOT NULL DEFAULT 'private',
      created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id, tenant_id),
      CHECK (visibility IN ('private','public')))
    """)
    op.execute("""CREATE TABLE iam.memberships (
      id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES iam.organizations(tenant_id) ON DELETE CASCADE,
      subject varchar(255) NOT NULL, role varchar(32) NOT NULL, status varchar(16) NOT NULL DEFAULT 'active',
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,id), UNIQUE(tenant_id,subject),
      CHECK (role IN ('org_owner','org_admin','principal_investigator','researcher','reviewer','viewer')),
      CHECK (status IN ('active','suspended','revoked')))
    """)


def downgrade() -> None:
    raise RuntimeError("P02 IAM downgrade is destructive and requires explicit data-loss review")
