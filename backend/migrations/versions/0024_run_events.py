"""Purpose: persist ordered P05 run events for polling and SSE replay.
Behavior: store tenant-scoped, redacted event payloads with per-run immutable sequences.
Refs: main_implementation plan.md P05.4; api design.md §5.3; Phase: P05.
"""

from alembic import op

revision = "0024_run_events"
down_revision = "0023_jobs_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE research.run_events (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,run_id uuid NOT NULL,
      sequence bigint NOT NULL CHECK(sequence>0),event_type varchar(96) NOT NULL,
      payload jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(tenant_id,project_id,id),UNIQUE(tenant_id,run_id,sequence),
      FOREIGN KEY(tenant_id,project_id,run_id) REFERENCES research.compute_runs(tenant_id,project_id,id))""")
    op.execute("ALTER TABLE research.run_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE research.run_events FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY run_events_tenant ON research.run_events
      USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
      WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT,INSERT ON research.run_events TO orca_runtime")
    op.execute("CREATE INDEX run_events_replay ON research.run_events(tenant_id,run_id,sequence)")


def downgrade() -> None:
    raise RuntimeError("Run event history requires explicit retention review")
