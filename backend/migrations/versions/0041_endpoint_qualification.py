"""Purpose: qualify secondary endpoints separately and pin research approvals to their target.
Behavior: preserve legacy primary routing and prevent inherited secondary qualification.
Refs: main_implementation plan.md P09.1/P09.3; api design.md §17.1; Phase: P09.
"""

from alembic import op

revision = "0041_endpoint_qualification"
down_revision = "0040_endpoint_check_target"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE federation.registration_jobs DROP CONSTRAINT registration_jobs_kind_check"
    )
    op.execute(
        "ALTER TABLE federation.registration_jobs DROP CONSTRAINT registration_job_endpoint_kind"
    )
    op.execute("""ALTER TABLE federation.registration_jobs ADD CONSTRAINT registration_jobs_kind_check
      CHECK(kind IN ('qualification','endpoint_check','endpoint_qualification')),
      ADD CONSTRAINT registration_job_endpoint_kind CHECK(
        (kind='qualification' AND endpoint IS NULL) OR kind='endpoint_check'
        OR (kind='endpoint_qualification' AND endpoint IS NOT NULL))""")
    op.execute("""CREATE TABLE federation.endpoint_qualifications(
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,agent_id uuid NOT NULL,binding_version uuid NOT NULL,
      endpoint varchar(500) NOT NULL,capability varchar(100) NOT NULL,
      report jsonb NOT NULL,expires_at timestamptz NOT NULL,
      FOREIGN KEY(tenant_id,agent_id,binding_version)
        REFERENCES federation.registration_versions(tenant_id,agent_id,id))""")
    op.execute("ALTER TABLE federation.endpoint_qualifications ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE federation.endpoint_qualifications FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY endpoint_qualifications_tenant ON federation.endpoint_qualifications
      USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
      WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT,INSERT ON federation.endpoint_qualifications TO orca_runtime")
    op.execute("""CREATE INDEX endpoint_qualification_expiry ON federation.endpoint_qualifications
      (tenant_id,agent_id,binding_version,endpoint,capability,expires_at DESC)""")
    op.execute("ALTER TABLE federation.research_authorizations ADD COLUMN endpoint varchar(500)")
    op.execute("ALTER TABLE federation.research_dispatches ADD COLUMN endpoint varchar(500)")
    op.execute("""DO $$
      DECLARE previous text;
      BEGIN
        SELECT conname INTO STRICT previous FROM pg_constraint
          WHERE conrelid='federation.research_authorizations'::regclass
            AND pg_get_constraintdef(oid) =
              'UNIQUE (tenant_id, project_id, agent_id, binding_version, capability_id, method_id)';
        EXECUTE format('ALTER TABLE federation.research_authorizations DROP CONSTRAINT %I',previous);
      END $$""")
    op.execute("""ALTER TABLE federation.research_authorizations
      ADD CONSTRAINT research_authorization_target_unique UNIQUE NULLS NOT DISTINCT
      (tenant_id,project_id,agent_id,binding_version,capability_id,method_id,endpoint)""")


def downgrade() -> None:
    raise RuntimeError(
        "Endpoint qualification and dispatch history require explicit recovery review"
    )
