"""Purpose: persist tenant-owned P03 catalog products and immutable source versions.
Behavior: enforce composite ownership, default-deny RLS and append-only versions.
Refs: main plan P03.1; API 3.4; research-only guide 4-5; Phase: P03.1.
"""

from alembic import op

revision = "0003_catalog"
down_revision = "0002_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE research.catalog_products (
      id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES iam.organizations(tenant_id),
      provider_id text NOT NULL, source_product_id text NOT NULL,
      kind text NOT NULL CHECK(kind IN ('observation','reanalysis','forecast','derived','static')),
      access_class text NOT NULL DEFAULT 'restricted' CHECK(access_class IN ('public','restricted')),
      metadata_publication_allowed boolean NOT NULL DEFAULT false,
      status text NOT NULL DEFAULT 'candidate' CHECK(status IN ('candidate','probe_passed','validated')),
      UNIQUE(tenant_id,id), UNIQUE(tenant_id,provider_id,source_product_id))""")
    op.execute("""CREATE TABLE research.catalog_versions (
      id uuid PRIMARY KEY, tenant_id uuid NOT NULL, product_id uuid NOT NULL,
      source_version text NOT NULL, metadata_digest text NOT NULL
        CHECK(metadata_digest ~ '^sha256:[0-9a-f]{64}$'),
      manifest jsonb NOT NULL CHECK(jsonb_typeof(manifest)='object'),
      created_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY(tenant_id,product_id) REFERENCES research.catalog_products(tenant_id,id),
      UNIQUE(tenant_id,product_id,source_version,metadata_digest))""")
    for name in ("catalog_products", "catalog_versions"):
        table = "research." + name
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY {name}_tenant ON {table}
          USING (tenant_id = nullif(current_setting('orca.tenant_id',true),'')::uuid)
          WITH CHECK (tenant_id = nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
        op.execute(f"GRANT SELECT, INSERT ON {table} TO orca_runtime")
        op.execute(f"CREATE INDEX ix_{name}_tenant_id ON {table}(tenant_id,id)")
    op.execute("""CREATE FUNCTION research.reject_catalog_version_mutation() RETURNS trigger
      LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'catalog versions are immutable'; END $$""")
    op.execute("""CREATE TRIGGER catalog_versions_immutable BEFORE UPDATE OR DELETE
      ON research.catalog_versions FOR EACH ROW EXECUTE FUNCTION
      research.reject_catalog_version_mutation()""")


def downgrade() -> None:
    raise RuntimeError("Catalog downgrade requires an explicit preservation/data-loss review")
