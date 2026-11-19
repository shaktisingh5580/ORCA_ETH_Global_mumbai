"""Purpose: retain each P09 job's selected endpoint.
Behavior: older endpoint jobs still resolve to the registration's primary endpoint.
Refs: main_implementation plan.md P09.1; api design.md §3.10; Phase: P09.
"""

from alembic import op

revision = "0040_endpoint_check_target"
down_revision = "0039_endpoint_check_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE federation.registration_jobs ADD COLUMN endpoint varchar(500)")
    op.execute("""ALTER TABLE federation.registration_jobs ADD CONSTRAINT
      registration_job_endpoint_kind CHECK(endpoint IS NULL OR kind='endpoint_check')""")


def downgrade() -> None:
    raise RuntimeError("Endpoint check target history requires explicit recovery review")
