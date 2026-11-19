"""Purpose: queue P09 endpoint checks beside durable onboarding qualifications.
Behavior: index job kinds and recent endpoint checks per registration.
Refs: main_implementation plan.md P09.1; api design.md §3.10; Phase: P09.
"""

from alembic import op

revision = "0039_endpoint_check_jobs"
down_revision = "0038_dispatch_correlation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE federation.registration_jobs ADD COLUMN kind text NOT NULL
      DEFAULT 'qualification' CHECK (kind IN ('qualification','endpoint_check'))""")
    op.execute("DROP INDEX federation.registration_one_live_job")
    op.execute("""CREATE UNIQUE INDEX registration_one_live_job
      ON federation.registration_jobs(tenant_id,agent_id,binding_version,kind)
      WHERE state IN ('queued','running')""")
    op.execute("""CREATE INDEX registration_endpoint_check_recent
      ON federation.registration_jobs(tenant_id,agent_id,created_at DESC)
      WHERE kind='endpoint_check'""")


def downgrade() -> None:
    raise RuntimeError("Endpoint check history requires explicit recovery review")
