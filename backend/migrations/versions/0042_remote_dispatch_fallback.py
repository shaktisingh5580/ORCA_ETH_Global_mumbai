"""Purpose: link a replacement P09 dispatch to a provably unsent attempt.
Behavior: enforce one replacement per dispatch within the same tenant, project and run.
Refs: main_implementation plan.md P09.3; api design.md §17.1; Phase: P09.
"""

from alembic import op

revision = "0042_remote_dispatch_fallback"
down_revision = "0041_endpoint_qualification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE federation.research_dispatches
      ADD COLUMN fallback_from uuid,
      ADD CONSTRAINT research_dispatch_same_run UNIQUE(tenant_id,project_id,run_id,id),
      ADD CONSTRAINT research_dispatch_one_replacement UNIQUE(fallback_from),
      ADD CONSTRAINT research_dispatch_fallback_origin
        FOREIGN KEY(tenant_id,project_id,run_id,fallback_from)
        REFERENCES federation.research_dispatches(tenant_id,project_id,run_id,id),
      ADD CONSTRAINT research_dispatch_fallback_not_self CHECK(fallback_from IS DISTINCT FROM id)""")


def downgrade() -> None:
    raise RuntimeError("Dispatch fallback history requires explicit recovery review")
