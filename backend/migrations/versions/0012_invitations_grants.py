"""Purpose: persist single-use invitations and expiring resource grants under tenant RLS.
Behavior: expose only bounded invitation lookup and include live read grants in project discovery.
Refs: API 2.2/3.2; main plan P02; Phase: P02 completion repair.
"""

from alembic import op

revision = "0012_invitations_grants"
down_revision = "0011_project_authorization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE iam.invitations (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL REFERENCES iam.organizations(tenant_id),
      email varchar(254) NOT NULL,role varchar(32) NOT NULL,expires_at timestamptz NOT NULL,
      token_hash varchar(64) NOT NULL UNIQUE,created_by varchar(255) NOT NULL,
      idempotency_key varchar(128) NOT NULL,request_hash varchar(64) NOT NULL,
      accepted_by varchar(255),accepted_at timestamptz,created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,idempotency_key), CHECK(token_hash ~ '^[0-9a-f]{64}$'),
      CHECK(role IN ('org_owner','org_admin','principal_investigator','researcher','reviewer','viewer','source_operator')))""")
    op.execute("""CREATE TABLE iam.access_grants (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,principal_id uuid NOT NULL,
      resource_version bigint NOT NULL,expires_at timestamptz NOT NULL,
      created_by varchar(255) NOT NULL,revoked boolean NOT NULL DEFAULT false,
      version bigint NOT NULL DEFAULT 1,created_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY(tenant_id,project_id) REFERENCES research.projects(tenant_id,id) ON DELETE CASCADE,
      FOREIGN KEY(tenant_id,principal_id) REFERENCES iam.memberships(tenant_id,id),
      UNIQUE(tenant_id,id))""")
    for table in ("invitations", "access_grants"):
        op.execute(f"ALTER TABLE iam.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE iam.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY {table}_tenant ON iam.{table}
          USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
          WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
        op.execute(f"GRANT SELECT,INSERT,UPDATE ON iam.{table} TO orca_runtime")
    op.execute("""CREATE FUNCTION iam.resolve_invitation_tenant(p_hash text,p_email text)
      RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,iam AS $$
      SELECT tenant_id FROM iam.invitations WHERE token_hash=p_hash AND email=p_email
      AND expires_at>clock_timestamp() AND accepted_at IS NULL LIMIT 1 $$""")
    op.execute("REVOKE ALL ON FUNCTION iam.resolve_invitation_tenant(text,text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION iam.resolve_invitation_tenant(text,text) TO orca_runtime")
    op.execute(
        "CREATE INDEX access_grants_lookup ON iam.access_grants(tenant_id,project_id,principal_id) WHERE NOT revoked"
    )
    op.execute("""CREATE OR REPLACE FUNCTION iam.resolve_project_tenant(p_subject text,p_project uuid)
      RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,iam,research AS $$
      SELECT p.tenant_id FROM research.projects p
      JOIN iam.memberships m ON m.tenant_id=p.tenant_id AND m.subject=p_subject AND m.status='active'
      WHERE p.id=p_project AND (m.role IN ('org_owner','org_admin') OR p.access_class IN ('public','organization') OR
        EXISTS(SELECT 1 FROM research.project_members pm WHERE pm.tenant_id=p.tenant_id
          AND pm.project_id=p.id AND pm.principal_id=m.id AND pm.status='active') OR
        EXISTS(SELECT 1 FROM iam.access_grants g WHERE g.tenant_id=p.tenant_id AND g.project_id=p.id
          AND g.principal_id=m.id AND NOT g.revoked AND g.expires_at>clock_timestamp()
          AND g.resource_version=p.version)) LIMIT 1 $$""")


def downgrade() -> None:
    raise RuntimeError("Invitation/grant downgrade requires explicit identity and audit review")
