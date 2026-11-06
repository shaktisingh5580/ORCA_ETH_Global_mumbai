"""Purpose: separate project authority from organization membership and persist bounded policy.
Behavior: add member versions, tenant policy RLS and a private-project-aware resolver.
Refs: API 2.2/3.2/3.3; main plan P02; Phase: P02 completion repair.
"""

from alembic import op

revision = "0011_project_authorization"
down_revision = "0010_organization_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE research.project_members ADD COLUMN version bigint NOT NULL DEFAULT 1")
    # Historical principal_id values were unconstrained. Enforce new writes without
    # silently deleting or reinterpreting existing identities during an upgrade.
    op.execute("""ALTER TABLE research.project_members ADD CONSTRAINT project_member_identity_fk
      FOREIGN KEY(tenant_id,principal_id) REFERENCES iam.memberships(tenant_id,id) NOT VALID""")
    op.execute("""ALTER TABLE research.project_members ADD CONSTRAINT project_member_role_check
      CHECK(role IN ('admin','editor','reviewer','viewer')) NOT VALID""")
    op.execute("""ALTER TABLE research.project_members ADD CONSTRAINT project_member_status_check
      CHECK(status IN ('active','revoked')) NOT VALID""")
    # Validate without deleting or reassigning legacy identities. An incompatible
    # existing row aborts this migration transaction and requires explicit repair.
    for constraint in (
        "project_member_identity_fk",
        "project_member_role_check",
        "project_member_status_check",
    ):
        op.execute(f"ALTER TABLE research.project_members VALIDATE CONSTRAINT {constraint}")
    op.execute("""CREATE TABLE iam.organization_policies (
      tenant_id uuid PRIMARY KEY REFERENCES iam.organizations(tenant_id) ON DELETE CASCADE,
      retention_days integer NOT NULL DEFAULT 365 CHECK(retention_days BETWEEN 30 AND 3650),
      max_upload_bytes bigint NOT NULL DEFAULT 10485760 CHECK(max_upload_bytes BETWEEN 1 AND 104857600),
      max_projects integer NOT NULL DEFAULT 100 CHECK(max_projects BETWEEN 1 AND 1000),
      version bigint NOT NULL DEFAULT 1, updated_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("ALTER TABLE iam.organization_policies ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE iam.organization_policies FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY organization_policies_tenant ON iam.organization_policies
      USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
      WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT,INSERT,UPDATE ON iam.organization_policies TO orca_runtime")
    op.execute("""CREATE OR REPLACE FUNCTION iam.resolve_project_tenant(p_subject text,p_project uuid)
      RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,iam,research AS $$
      SELECT p.tenant_id FROM research.projects p
      JOIN iam.memberships m ON m.tenant_id=p.tenant_id AND m.subject=p_subject AND m.status='active'
      WHERE p.id=p_project AND (
        m.role IN ('org_owner','org_admin') OR p.access_class IN ('public','organization') OR
        EXISTS(SELECT 1 FROM research.project_members pm WHERE pm.tenant_id=p.tenant_id
          AND pm.project_id=p.id AND pm.principal_id=m.id AND pm.status='active')) LIMIT 1 $$""")
    op.execute("REVOKE ALL ON FUNCTION iam.resolve_project_tenant(text,uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION iam.resolve_project_tenant(text,uuid) TO orca_runtime")
    op.execute("""CREATE FUNCTION iam.resolve_upload_tenant(p_subject text,p_upload uuid)
      RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,iam,research AS $$
      SELECT u.tenant_id FROM research.uploads u WHERE u.id=p_upload
        AND (u.finalized OR u.metadata_json->>'owner'=p_subject)
        AND iam.resolve_project_tenant(p_subject,u.project_id)=u.tenant_id LIMIT 1 $$""")
    op.execute("REVOKE ALL ON FUNCTION iam.resolve_upload_tenant(text,uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION iam.resolve_upload_tenant(text,uuid) TO orca_runtime")
    op.execute("""CREATE FUNCTION iam.pending_uploads() RETURNS TABLE(tenant_id uuid,upload_id uuid)
      LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,research AS $$
      SELECT u.tenant_id,u.id FROM research.uploads u WHERE u.status='pending_validation'
      OR (u.metadata_json->>'staging_deleted'='false' AND u.metadata_json ? 'staging_key'
        AND (u.metadata_json->>'expires_at')::timestamptz<=clock_timestamp())
      ORDER BY u.updated_at,u.id LIMIT 100 $$""")
    op.execute("REVOKE ALL ON FUNCTION iam.pending_uploads() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION iam.pending_uploads() TO orca_runtime")


def downgrade() -> None:
    raise RuntimeError("Project authorization downgrade requires explicit security and data review")
