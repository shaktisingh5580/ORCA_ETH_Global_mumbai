"""Purpose: retain signed P09 grants and verified remote replies across worker crashes.
Behavior: expose an explicit reconciliation state instead of blindly replaying expired leases.
Refs: main_implementation plan.md P09.3; api design.md §17.1; Phase: P09.
"""

from alembic import op

revision = "0035_remote_dispatch_recovery"
down_revision = "0034_remote_research_dispatches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE federation.research_dispatches ALTER COLUMN state TYPE varchar(32)")
    op.execute("ALTER TABLE federation.research_dispatches ADD COLUMN signed_grant jsonb")
    op.execute("ALTER TABLE federation.research_dispatches ADD COLUMN verified_response jsonb")
    op.execute("ALTER TABLE federation.research_dispatches DROP CONSTRAINT research_dispatches_state_check")
    op.execute("""ALTER TABLE federation.research_dispatches ADD CONSTRAINT research_dispatches_state_check
      CHECK(state IN ('queued','leased','completed','failed','cancelled','reconciliation_required'))""")
    op.execute("""ALTER TABLE federation.research_dispatches ADD CONSTRAINT research_dispatch_response_check
      CHECK((state='completed')=(verified_response IS NOT NULL AND result_digest IS NOT NULL))""")


def downgrade() -> None:
    raise RuntimeError("Signed remote grant and response history require explicit recovery review")
