"""Purpose: complete tenant-safe organization bootstrap, versions and membership audit storage.
Behavior: restrict bootstrap execution, bind retries to subjects and enforce tenant RLS.
Refs: API 3.2; main plan P02; platform completion audit; Phase: P02 repair.
"""

from alembic import op

revision = "0010_organization_lifecycle"
down_revision = "0009_identity_directory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE iam.organizations ADD COLUMN version bigint NOT NULL DEFAULT 1, ADD COLUMN settings jsonb NOT NULL DEFAULT '{}', ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now()"
    )
    op.execute("ALTER TABLE iam.memberships ADD COLUMN version bigint NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE iam.organizations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE iam.organizations FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY organizations_tenant ON iam.organizations
      USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
      WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT,UPDATE ON iam.organizations TO orca_runtime")
    op.execute("""CREATE TABLE iam.organization_creation_keys (
      subject text NOT NULL,idempotency_key varchar(128) NOT NULL,request_hash char(64) NOT NULL,
      organization_id uuid NOT NULL REFERENCES iam.organizations(id),
      PRIMARY KEY(subject,idempotency_key))""")
    op.execute("ALTER TABLE iam.organization_creation_keys ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE iam.organization_creation_keys FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE TABLE iam.membership_audit (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL REFERENCES iam.organizations(tenant_id),
      actor_subject text NOT NULL,member_id uuid NOT NULL,event jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("ALTER TABLE iam.membership_audit ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE iam.membership_audit FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY membership_audit_tenant ON iam.membership_audit
      USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
      WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT,INSERT ON iam.membership_audit TO orca_runtime")
    op.execute("""CREATE TRIGGER membership_audit_immutable BEFORE UPDATE OR DELETE ON iam.membership_audit
      FOR EACH ROW EXECUTE FUNCTION research.reject_catalog_version_mutation()""")
    op.execute("""CREATE FUNCTION iam.bootstrap_organization(
      p_subject text,p_key text,p_hash text,p_id uuid,p_tenant uuid,p_member uuid,p_name text,p_slug text,p_visibility text)
      RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,iam AS $$
      DECLARE prior iam.organization_creation_keys%ROWTYPE;
      BEGIN
        IF length(p_subject)<1 OR length(p_subject)>255 OR length(p_key)<1 OR length(p_key)>128
          OR p_hash !~ '^[0-9a-f]{64}$' THEN RAISE EXCEPTION 'invalid bootstrap request'; END IF;
        PERFORM pg_advisory_xact_lock(hashtextextended(p_subject || ':' || p_key,0));
        SELECT * INTO prior FROM iam.organization_creation_keys WHERE subject=p_subject AND idempotency_key=p_key;
        IF FOUND THEN
          IF prior.request_hash<>p_hash THEN RETURN NULL; END IF;
          RETURN prior.organization_id;
        END IF;
        PERFORM set_config('orca.tenant_id',p_tenant::text,true);
        INSERT INTO iam.organizations(id,tenant_id,name,slug,visibility) VALUES(p_id,p_tenant,p_name,p_slug,p_visibility);
        INSERT INTO iam.memberships(id,tenant_id,subject,role) VALUES(p_member,p_tenant,p_subject,'org_owner');
        INSERT INTO iam.organization_creation_keys VALUES(p_subject,p_key,p_hash,p_id);
        RETURN p_id;
      END $$""")
    op.execute(
        "REVOKE ALL ON FUNCTION iam.bootstrap_organization(text,text,text,uuid,uuid,uuid,text,text,text) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION iam.bootstrap_organization(text,text,text,uuid,uuid,uuid,text,text,text) TO orca_runtime"
    )


def downgrade() -> None:
    raise RuntimeError(
        "Organization lifecycle downgrade requires explicit access/data retention review"
    )
