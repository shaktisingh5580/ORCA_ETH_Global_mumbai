"""Purpose: serialize final remote result acceptance with policy and source revocation.
Behavior: take read locks on active grants while preserving runtime read-only policy access.
Refs: main_implementation plan.md P09.3; api design.md §17.1; Phase: P09.
"""

from alembic import op

revision = "0037_dispatch_authority_lock"
down_revision = "0036_remote_cancellation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE FUNCTION federation.lock_dispatch_authority(dispatch_id uuid)
      RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
      DECLARE intent federation.research_dispatches%ROWTYPE;
              source_id uuid;
      BEGIN
        SELECT d.* INTO intent FROM federation.research_dispatches d
          WHERE d.id=dispatch_id
            AND d.tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid
            AND d.state IN ('leased','reconciliation_required');
        IF NOT FOUND THEN RETURN false; END IF;

        PERFORM 1 FROM federation.research_authorizations a
          WHERE a.id=intent.authorization_id AND a.tenant_id=intent.tenant_id
            AND a.project_id=intent.project_id AND a.organization_id=intent.organization_id
            AND a.agent_id=intent.agent_id AND a.binding_version=intent.binding_version
            AND a.capability_id=intent.capability_id AND a.method_id=intent.method_id
            AND a.revoked_at IS NULL AND a.expires_at>clock_timestamp()
          FOR SHARE;
        IF NOT FOUND THEN RETURN false; END IF;

        FOR source_id IN SELECT unnest(intent.dataset_ids) AS id ORDER BY id LOOP
          PERFORM 1 FROM research.snapshot_project_grants g
            WHERE g.tenant_id=intent.tenant_id AND g.project_id=intent.project_id
              AND g.snapshot_id=source_id AND (g.expires_at IS NULL OR g.expires_at>clock_timestamp())
            FOR SHARE;
          IF NOT FOUND THEN RETURN false; END IF;
        END LOOP;
        RETURN true;
      END $$""")
    op.execute("REVOKE ALL ON FUNCTION federation.lock_dispatch_authority(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION federation.lock_dispatch_authority(uuid) TO orca_runtime")


def downgrade() -> None:
    op.execute("DROP FUNCTION federation.lock_dispatch_authority(uuid)")
