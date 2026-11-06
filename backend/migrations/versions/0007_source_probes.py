"""Purpose: persist recoverable operator source probes with tenant isolation.
Behavior: enforce pinned distribution ownership, idempotency and fenced execution state.
Refs: API 3.4 source-probes; main plan P03.2/P03.4; Phase: P03.
"""

from alembic import op

revision = "0007_source_probes"
down_revision = "0006_snapshot_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE iam.memberships DROP CONSTRAINT memberships_role_check")
    op.execute("""ALTER TABLE iam.memberships ADD CONSTRAINT memberships_role_check
      CHECK(role IN ('org_owner','org_admin','principal_investigator','researcher','reviewer','viewer','source_operator'))""")
    op.execute("""CREATE TABLE research.source_probes (
      id uuid PRIMARY KEY, tenant_id uuid NOT NULL, version_id uuid NOT NULL,
      idempotency_key varchar(128) NOT NULL, request_hash char(64) NOT NULL, payload jsonb NOT NULL,
      status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','succeeded','failed')),
      attempts integer NOT NULL DEFAULT 0, lease_token uuid, lease_until timestamptz,
      error_code text, evidence jsonb,
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,idempotency_key),
      FOREIGN KEY(tenant_id,version_id) REFERENCES research.catalog_versions(tenant_id,id),
      CHECK((status='succeeded')=(evidence IS NOT NULL)))""")
    op.execute(
        "CREATE INDEX ix_source_probe_claim ON research.source_probes(tenant_id,status,lease_until)"
    )
    op.execute("ALTER TABLE research.source_probes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE research.source_probes FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY source_probes_tenant ON research.source_probes
      USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
      WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT,INSERT,UPDATE ON research.source_probes TO orca_runtime")


def downgrade() -> None:
    raise RuntimeError("Source probe downgrade requires explicit evidence retention review")
