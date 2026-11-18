"""Purpose: retain a private P09 correlation history for remote research dispatches.
Behavior: append state and reference changes atomically with each dispatch write.
Refs: main_implementation plan.md P09.4; api design.md §13; Phase: P09.
"""

from alembic import op

revision = "0038_dispatch_correlation"
down_revision = "0037_dispatch_authority_lock"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE federation.research_dispatch_events (
      tenant_id uuid NOT NULL,project_id uuid NOT NULL,dispatch_id uuid NOT NULL,
      sequence bigint NOT NULL,run_id uuid NOT NULL,agent_id uuid NOT NULL,
      binding_version uuid NOT NULL,generation bigint NOT NULL,kind text NOT NULL,
      state varchar(32) NOT NULL,grant_digest text,result_digest text,
      remote_task_id varchar(256),remote_context_id varchar(256),
      error_code varchar(96),remote_cancellation_confirmed boolean NOT NULL,
      observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      PRIMARY KEY(tenant_id,project_id,dispatch_id,sequence),
      FOREIGN KEY(tenant_id,project_id,dispatch_id)
        REFERENCES federation.research_dispatches(tenant_id,project_id,id))""")
    op.execute("ALTER TABLE federation.research_dispatch_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE federation.research_dispatch_events FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY research_dispatch_events_tenant
      ON federation.research_dispatch_events
      USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
      WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT ON federation.research_dispatch_events TO orca_runtime")
    op.execute("""CREATE FUNCTION federation.append_research_dispatch_event()
      RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
      DECLARE event_kind text;
      BEGIN
        IF TG_OP='INSERT' THEN event_kind := 'created';
        ELSIF NEW.state IS DISTINCT FROM OLD.state THEN event_kind := 'state_changed';
        ELSIF NEW.remote_task_id IS DISTINCT FROM OLD.remote_task_id THEN event_kind := 'task_observed';
        ELSIF NEW.grant_digest IS DISTINCT FROM OLD.grant_digest THEN event_kind := 'grant_persisted';
        ELSIF NEW.remote_cancel_confirmed_at IS DISTINCT FROM OLD.remote_cancel_confirmed_at
          THEN event_kind := 'cancellation_confirmed';
        ELSIF NEW.remote_cancel_attempted_at IS DISTINCT FROM OLD.remote_cancel_attempted_at
          THEN event_kind := 'cancellation_attempted';
        ELSE RETURN NEW;
        END IF;
        INSERT INTO federation.research_dispatch_events
          (tenant_id,project_id,dispatch_id,sequence,run_id,agent_id,binding_version,generation,kind,
           state,grant_digest,result_digest,remote_task_id,remote_context_id,error_code,
           remote_cancellation_confirmed)
        VALUES (NEW.tenant_id,NEW.project_id,NEW.id,
          (SELECT coalesce(max(e.sequence),0)+1 FROM federation.research_dispatch_events e
            WHERE e.tenant_id=NEW.tenant_id AND e.project_id=NEW.project_id AND e.dispatch_id=NEW.id),
          NEW.run_id,NEW.agent_id,NEW.binding_version,NEW.generation,event_kind,
          NEW.state,NEW.grant_digest,NEW.result_digest,NEW.remote_task_id,NEW.remote_context_id,
          NEW.error_code,NEW.remote_cancel_confirmed_at IS NOT NULL);
        RETURN NEW;
      END $$""")
    op.execute("REVOKE ALL ON FUNCTION federation.append_research_dispatch_event() FROM PUBLIC")
    op.execute("""CREATE TRIGGER research_dispatch_events_append
      AFTER INSERT OR UPDATE ON federation.research_dispatches
      FOR EACH ROW EXECUTE FUNCTION federation.append_research_dispatch_event()""")


def downgrade() -> None:
    raise RuntimeError("Dispatch correlation history requires explicit recovery review")
