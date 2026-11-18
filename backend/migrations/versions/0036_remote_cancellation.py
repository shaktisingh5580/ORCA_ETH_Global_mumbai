"""Purpose: preserve P09 remote task identity and cancellation observations.
Behavior: retain the provider's A2A task reference and a single cancellation attempt.
Refs: main_implementation plan.md P09.2-P09.3; api design.md §17.1; Phase: P09.
"""

from alembic import op

revision = "0036_remote_cancellation"
down_revision = "0035_remote_dispatch_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE federation.research_dispatches
      ADD COLUMN remote_task_id varchar(256),
      ADD COLUMN remote_context_id varchar(256),
      ADD COLUMN remote_cancel_attempted_at timestamptz,
      ADD COLUMN remote_cancel_confirmed_at timestamptz,
      ADD CONSTRAINT research_dispatch_remote_ref CHECK
        ((remote_task_id IS NULL)=(remote_context_id IS NULL)),
      ADD CONSTRAINT research_dispatch_remote_cancel CHECK
        (remote_cancel_confirmed_at IS NULL OR (state='cancelled'
          AND remote_cancel_attempted_at IS NOT NULL AND remote_task_id IS NOT NULL))""")


def downgrade() -> None:
    raise RuntimeError("Remote cancellation history requires explicit recovery review")
