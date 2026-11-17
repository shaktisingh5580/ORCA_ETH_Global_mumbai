"""Purpose: retain P07 benchmark scores and their limits as immutable project evidence.
Behavior: link every result to a frozen benchmark and exact prediction/reference digests.
Refs: main_implementation plan.md P07.4; api design.md §4.5; Phase: P07.
"""

from alembic import op

revision = "0031_benchmark_evaluations"
down_revision = "0030_benchmark_definitions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE research.benchmark_evaluations (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,
      benchmark_id uuid NOT NULL,
      prediction_digest text NOT NULL CHECK(prediction_digest ~ '^sha256:[0-9a-f]{64}$'),
      reference_digest text NOT NULL CHECK(reference_digest ~ '^sha256:[0-9a-f]{64}$'),
      metric_algorithm varchar(40) NOT NULL CHECK(metric_algorithm='mae_rmse_bias_v1'),
      status varchar(24) NOT NULL CHECK(status IN ('evaluated','not_evaluable','blocked')),
      sample_count integer NOT NULL CHECK(sample_count>=0),
      mae double precision,rmse double precision,bias double precision,
      synthetic boolean NOT NULL,reference_independence_verified boolean NOT NULL DEFAULT false,
      reason text,limitations jsonb NOT NULL CHECK(jsonb_typeof(limitations)='array'),
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      UNIQUE(tenant_id,project_id,id),
      FOREIGN KEY(tenant_id,project_id,benchmark_id)
        REFERENCES research.benchmark_definitions(tenant_id,project_id,id),
      CHECK(reference_independence_verified=false),
      CHECK((status='evaluated' AND sample_count>0 AND mae IS NOT NULL
        AND rmse IS NOT NULL AND bias IS NOT NULL AND reason IS NULL)
        OR (status<>'evaluated' AND sample_count=0 AND mae IS NULL
        AND rmse IS NULL AND bias IS NULL AND reason IS NOT NULL))
    )""")
    op.execute("ALTER TABLE research.benchmark_evaluations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE research.benchmark_evaluations FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY benchmark_evaluations_tenant ON research.benchmark_evaluations
      USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
      WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT,INSERT ON research.benchmark_evaluations TO orca_runtime")
    op.execute("""CREATE TRIGGER benchmark_evaluations_immutable
      BEFORE UPDATE OR DELETE ON research.benchmark_evaluations
      FOR EACH ROW EXECUTE FUNCTION research.protect_tabular_evidence()""")
    op.execute("""CREATE INDEX benchmark_evaluations_benchmark
      ON research.benchmark_evaluations(tenant_id,project_id,benchmark_id,created_at)""")


def downgrade() -> None:
    raise RuntimeError("Benchmark evaluation evidence requires explicit retention review")
