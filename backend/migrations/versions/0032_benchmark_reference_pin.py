"""Purpose: bind P07 evaluation rows to the frozen benchmark digest and synthetic status.
Behavior: reject a score that cites another reference set or mislabels synthetic truth.
Refs: main_implementation plan.md P07.4; api design.md §4.5; Phase: P07.
"""

from alembic import op

revision = "0032_benchmark_reference_pin"
down_revision = "0031_benchmark_evaluations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE research.benchmark_definitions
      ADD CONSTRAINT benchmark_definitions_evaluation_pin
      UNIQUE(tenant_id,project_id,id,frozen_test_set_digest,synthetic)""")
    op.execute("""ALTER TABLE research.benchmark_evaluations
      ADD CONSTRAINT benchmark_evaluations_reference_pin
      FOREIGN KEY(tenant_id,project_id,benchmark_id,reference_digest,synthetic)
      REFERENCES research.benchmark_definitions
        (tenant_id,project_id,id,frozen_test_set_digest,synthetic)""")


def downgrade() -> None:
    raise RuntimeError("Frozen benchmark reference linkage requires explicit retention review")
