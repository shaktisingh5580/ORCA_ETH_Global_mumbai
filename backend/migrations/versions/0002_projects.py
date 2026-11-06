"""Purpose: create tenant-keyed projects, memberships and staged uploads.
Behavior: enforce composite tenant FKs and install default-deny RLS on private rows.
Refs: main plan P02.1/P02.3/P02.5; API section 3.3; Phase: P02.
"""

from alembic import op

revision = "0002_projects"
down_revision = "0001_iam"
branch_labels = None
depends_on = None

PRIVATE_TABLES = (
    "iam.memberships",
    "iam.idempotency_records",
    "research.projects",
    "research.project_members",
    "research.uploads",
)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS research")
    op.execute("""CREATE TABLE iam.idempotency_records (
      tenant_id uuid NOT NULL REFERENCES iam.organizations(tenant_id) ON DELETE CASCADE,
      operation varchar(80) NOT NULL, idempotency_key varchar(128) NOT NULL,
      request_hash varchar(64) NOT NULL, resource_id uuid NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(tenant_id,operation,idempotency_key))
    """)
    op.execute("""CREATE TABLE research.projects (
      id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES iam.organizations(tenant_id) ON DELETE RESTRICT,
      slug varchar(63) NOT NULL, name varchar(200) NOT NULL, description text,
      access_class varchar(24) NOT NULL DEFAULT 'project', state varchar(16) NOT NULL DEFAULT 'active',
      version bigint NOT NULL DEFAULT 1, created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(tenant_id,id), UNIQUE(tenant_id,slug),
      CHECK (access_class IN ('public','organization','project','restricted','confidential')),
      CHECK (state IN ('active','archived')))
    """)
    op.execute("""CREATE TABLE research.project_members (
      id uuid PRIMARY KEY, tenant_id uuid NOT NULL, project_id uuid NOT NULL, principal_id uuid NOT NULL,
      role varchar(32) NOT NULL, status varchar(16) NOT NULL DEFAULT 'active', created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(tenant_id,id), UNIQUE(tenant_id,project_id,principal_id),
      FOREIGN KEY(tenant_id,project_id) REFERENCES research.projects(tenant_id,id) ON DELETE CASCADE)
    """)
    op.execute("""CREATE TABLE research.uploads (
      id uuid PRIMARY KEY, tenant_id uuid NOT NULL, project_id uuid NOT NULL, object_key varchar(1024) NOT NULL UNIQUE,
      filename varchar(255) NOT NULL, media_type varchar(127) NOT NULL, size_bytes bigint NOT NULL,
      sha256 varchar(64) NOT NULL, classification varchar(24) NOT NULL, status varchar(24) NOT NULL DEFAULT 'staging',
      object_version_id varchar(255), metadata_json jsonb NOT NULL DEFAULT '{}', finalized boolean NOT NULL DEFAULT false,
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(tenant_id,id),
      FOREIGN KEY(tenant_id,project_id) REFERENCES research.projects(tenant_id,id) ON DELETE CASCADE,
      CHECK(size_bytes >= 0), CHECK(sha256 ~ '^[0-9a-f]{64}$'),
      CHECK(status IN ('staging','uploaded','pending_validation','finalized','rejected','aborted')))
    """)
    op.execute("CREATE INDEX ix_projects_tenant_state ON research.projects(tenant_id,state,id)")
    op.execute(
        "CREATE INDEX ix_upload_project_status ON research.uploads(tenant_id,project_id,status)"
    )
    for table in PRIVATE_TABLES:
        policy = table.replace(".", "_") + "_tenant"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {policy} ON {table} USING (tenant_id = nullif(current_setting('orca.tenant_id', true), '')::uuid) WITH CHECK (tenant_id = nullif(current_setting('orca.tenant_id', true), '')::uuid)"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO orca_runtime")
    op.execute("GRANT USAGE ON SCHEMA iam,research TO orca_runtime")
    op.execute("""CREATE OR REPLACE FUNCTION iam.resolve_tenant(p_subject text, p_organization uuid)
      RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,iam AS $$
      SELECT o.tenant_id FROM iam.organizations o JOIN iam.memberships m ON m.tenant_id=o.tenant_id
      WHERE o.id=p_organization AND m.subject=p_subject AND m.status='active' LIMIT 1 $$""")
    op.execute("REVOKE ALL ON FUNCTION iam.resolve_tenant(text,uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION iam.resolve_tenant(text,uuid) TO orca_runtime")
    op.execute("""CREATE OR REPLACE FUNCTION iam.resolve_project_tenant(p_subject text, p_project uuid)
      RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,iam,research AS $$
      SELECT p.tenant_id FROM research.projects p JOIN iam.memberships m ON m.tenant_id=p.tenant_id
      WHERE p.id=p_project AND m.subject=p_subject AND m.status='active' LIMIT 1 $$""")
    op.execute("REVOKE ALL ON FUNCTION iam.resolve_project_tenant(text,uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION iam.resolve_project_tenant(text,uuid) TO orca_runtime")


def downgrade() -> None:
    raise RuntimeError(
        "P02 project downgrade is destructive and requires explicit data-loss review"
    )
