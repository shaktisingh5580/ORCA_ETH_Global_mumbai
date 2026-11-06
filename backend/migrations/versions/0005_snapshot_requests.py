"""Purpose: store durable P03 acquisition requests and fence retrying workers.
Behavior: enforce tenant FKs, idempotency and default-deny RLS on request state.
Refs: API 3.4; main plan P03.3/P03.5; Phase: P03.5.
"""

from alembic import op

revision = "0005_snapshot_requests"
down_revision = "0004_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE research.dataset_snapshots ADD CONSTRAINT snapshots_tenant_id_unique UNIQUE(tenant_id,id)"
    )
    op.execute("""CREATE TABLE research.snapshot_requests (
      id uuid PRIMARY KEY, tenant_id uuid NOT NULL, version_id uuid NOT NULL,
      idempotency_key varchar(128) NOT NULL, request_hash char(64) NOT NULL,
      payload jsonb NOT NULL, status text NOT NULL DEFAULT 'queued'
        CHECK(status IN ('queued','running','succeeded','failed')),
      attempts integer NOT NULL DEFAULT 0, lease_token uuid, lease_until timestamptz,
      snapshot_id uuid, error_code varchar(80),
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,idempotency_key),
      FOREIGN KEY(tenant_id,version_id) REFERENCES research.catalog_versions(tenant_id,id),
      FOREIGN KEY(tenant_id,snapshot_id) REFERENCES research.dataset_snapshots(tenant_id,id),
      CHECK((status='succeeded')=(snapshot_id IS NOT NULL)))""")
    op.execute(
        "CREATE INDEX ix_snapshot_requests_claim ON research.snapshot_requests(tenant_id,status,lease_until)"
    )
    op.execute("ALTER TABLE research.snapshot_requests ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE research.snapshot_requests FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY snapshot_requests_tenant ON research.snapshot_requests
      USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
      WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT,INSERT,UPDATE ON research.snapshot_requests TO orca_runtime")


def downgrade() -> None:
    raise RuntimeError("Durable request downgrade requires explicit recovery review")
