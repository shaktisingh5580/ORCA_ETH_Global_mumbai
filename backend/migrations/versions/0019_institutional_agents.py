"""Purpose: persist organization-owned agent versions, proofs and qualification evidence.
Behavior: enforce tenant isolation and immutable revisions alongside existing pinned providers.
Refs: institutional API/data design section 7; Phase: F02.1.
"""

from alembic import op

revision = "0019_institutional_agents"
down_revision = "0018_research_anchors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE federation.registrations(
      id uuid PRIMARY KEY, tenant_id uuid NOT NULL, organization_id uuid NOT NULL,
      created_by text NOT NULL, idempotency_key text NOT NULL, request_digest text NOT NULL,
      version integer NOT NULL DEFAULT 1 CHECK(version>0),
      state text NOT NULL DEFAULT 'draft' CHECK(state IN
        ('draft','verification_pending','verified','qualified','active','suspended','revoked')),
      binding_version uuid NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,organization_id,id),
      UNIQUE(tenant_id,organization_id,created_by,idempotency_key),
      FOREIGN KEY(organization_id,tenant_id) REFERENCES iam.organizations(id,tenant_id))""")
    op.execute("""CREATE TABLE federation.registration_versions(
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,organization_id uuid NOT NULL,
      agent_id uuid NOT NULL,registration jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,agent_id,id),
      FOREIGN KEY(tenant_id,organization_id,agent_id)
        REFERENCES federation.registrations(tenant_id,organization_id,id))""")
    op.execute("""CREATE TABLE federation.registration_challenges(
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,organization_id uuid NOT NULL,
      agent_id uuid NOT NULL,binding_version uuid NOT NULL,payload jsonb NOT NULL,
      expires_at timestamptz NOT NULL,consumed_at timestamptz,signature text,
      FOREIGN KEY(tenant_id,organization_id,agent_id)
        REFERENCES federation.registrations(tenant_id,organization_id,id))""")
    op.execute("""CREATE TABLE federation.agent_qualifications(
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,organization_id uuid NOT NULL,
      agent_id uuid NOT NULL,binding_version uuid NOT NULL,report jsonb NOT NULL,
      expires_at timestamptz NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY(tenant_id,organization_id,agent_id)
        REFERENCES federation.registrations(tenant_id,organization_id,id))""")
    op.execute("""CREATE TABLE federation.registration_events(
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,organization_id uuid NOT NULL,
      agent_id uuid NOT NULL,actor text NOT NULL,event_type text NOT NULL,data jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY(tenant_id,organization_id,agent_id)
        REFERENCES federation.registrations(tenant_id,organization_id,id))""")
    op.execute("""CREATE TABLE federation.registration_jobs(
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,organization_id uuid NOT NULL,agent_id uuid NOT NULL,
      actor text NOT NULL,idempotency_key text NOT NULL,expected_version integer NOT NULL,
      binding_version uuid NOT NULL,state text NOT NULL DEFAULT 'queued'
        CHECK(state IN ('queued','running','succeeded','failed')),
      report jsonb,error_code text,lease_token uuid,lease_until timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,organization_id,actor,idempotency_key),
      FOREIGN KEY(tenant_id,organization_id,agent_id)
        REFERENCES federation.registrations(tenant_id,organization_id,id))""")
    for table in (
        "registrations",
        "registration_versions",
        "registration_challenges",
        "agent_qualifications",
        "registration_events",
        "registration_jobs",
    ):
        op.execute(f"ALTER TABLE federation.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE federation.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY {table}_tenant ON federation.{table}
          USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
          WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
        permissions = (
            "SELECT,INSERT,UPDATE"
            if table in {"registrations", "registration_challenges", "registration_jobs"}
            else "SELECT,INSERT"
        )
        op.execute(f"GRANT {permissions} ON federation.{table} TO orca_runtime")
    op.execute(
        "CREATE INDEX registrations_org_state ON federation.registrations(tenant_id,organization_id,state)"
    )
    op.execute("""CREATE UNIQUE INDEX registration_one_live_job
      ON federation.registration_jobs(tenant_id,agent_id,binding_version)
      WHERE state IN ('queued','running')""")
    op.execute("""CREATE FUNCTION iam.pending_registration_jobs() RETURNS TABLE(tenant_id uuid,job_id uuid)
      LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,federation AS $$
      SELECT tenant_id,id FROM federation.registration_jobs WHERE state='queued'
      OR (state='running' AND lease_until<now()) ORDER BY created_at LIMIT 10 $$""")
    op.execute("REVOKE ALL ON FUNCTION iam.pending_registration_jobs() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION iam.pending_registration_jobs() TO orca_runtime")
    op.execute("""CREATE FUNCTION iam.discover_institutional_agents() RETURNS SETOF jsonb
      LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,federation AS $$
      SELECT jsonb_build_object('id',r.id,'name',v.registration->>'name',
        'capability',v.registration->>'capability','endpoint',v.registration->>'endpoint',
        'operator_mode',v.registration->>'operator_mode','identity_status','service_only_not_ens_verified',
        'affiliation_status','self_declared','research_dispatch_enabled',false)
      FROM federation.registrations r JOIN federation.registration_versions v
        ON v.id=r.binding_version AND v.tenant_id=r.tenant_id
      WHERE r.state='active' AND v.registration->>'discoverable'='true'
        AND EXISTS(SELECT 1 FROM federation.agent_qualifications q
          WHERE q.tenant_id=r.tenant_id AND q.agent_id=r.id AND q.binding_version=r.binding_version
          AND q.expires_at>now() AND q.report->>'status'='PASS') LIMIT 100 $$""")
    op.execute("REVOKE ALL ON FUNCTION iam.discover_institutional_agents() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION iam.discover_institutional_agents() TO orca_runtime")


def downgrade() -> None:
    raise RuntimeError("Agent proof and participation retention requires review")
