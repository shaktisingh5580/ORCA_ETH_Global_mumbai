"""Purpose: freeze P07 benchmark identity and reference policy without storing answer values in API metadata.
Behavior: enforce tenant/project isolation and immutable, digest-pinned definitions.
Refs: main_implementation plan.md P07.4; api design.md §§3.8, 4.5; Phase: P07.
"""

from alembic import op

revision = "0030_benchmark_definitions"
down_revision = "0029_historical_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE research.benchmark_definitions (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,
      title varchar(200) NOT NULL CHECK(length(trim(title))>0),
      domain varchar(80) NOT NULL CHECK(length(trim(domain))>0),
      method_id varchar(128) NOT NULL CHECK(length(trim(method_id))>0),
      version varchar(80) NOT NULL CHECK(length(trim(version))>0),
      frozen_test_set_digest text NOT NULL CHECK(frozen_test_set_digest ~ '^sha256:[0-9a-f]{64}$'),
      reference_policy_digest text NOT NULL CHECK(reference_policy_digest ~ '^sha256:[0-9a-f]{64}$'),
      reference_kind varchar(32) NOT NULL
        CHECK(reference_kind IN ('independent_observation','synthetic_fixture')),
      synthetic boolean NOT NULL,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      UNIQUE(tenant_id,project_id,id),
      UNIQUE(tenant_id,project_id,domain,method_id,version),
      FOREIGN KEY(tenant_id,project_id) REFERENCES research.projects(tenant_id,id),
      CHECK(synthetic=(reference_kind='synthetic_fixture'))
    )""")
    op.execute("ALTER TABLE research.benchmark_definitions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE research.benchmark_definitions FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY benchmark_definitions_tenant ON research.benchmark_definitions
      USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
      WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT,INSERT ON research.benchmark_definitions TO orca_runtime")
    op.execute("""CREATE TRIGGER benchmark_definitions_immutable
      BEFORE UPDATE OR DELETE ON research.benchmark_definitions
      FOR EACH ROW EXECUTE FUNCTION research.protect_tabular_evidence()""")
    op.execute("""CREATE INDEX benchmark_definitions_catalog
      ON research.benchmark_definitions(tenant_id,project_id,domain,method_id,id)""")


def downgrade() -> None:
    raise RuntimeError("Frozen benchmark definitions require explicit retention review")
