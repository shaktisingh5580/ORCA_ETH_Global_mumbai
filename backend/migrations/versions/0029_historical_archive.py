"""Purpose: retain P07 historical source availability and exact-byte archival proof.
Behavior: append only server-timed observations linked to an immutable snapshot; no retrospective clock writes.
Refs: main_implementation plan.md P07.3; api design.md §§3.8, 4.5; Phase: P07.
"""

from alembic import op

revision = "0029_historical_archive"
down_revision = "0028_challenge_resolutions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE research.historical_archive_entries (
      id uuid PRIMARY KEY,tenant_id uuid NOT NULL,snapshot_id uuid NOT NULL,
      source_id uuid NOT NULL,content_digest text NOT NULL
        CHECK(content_digest ~ '^sha256:[0-9a-f]{64}$'),
      request_digest text NOT NULL CHECK(request_digest ~ '^sha256:[0-9a-f]{64}$'),
      byte_count bigint NOT NULL CHECK(byte_count>0),
      object_version_id text NOT NULL CHECK(object_version_id=content_digest),
      retention_kind varchar(32) NOT NULL CHECK(retention_kind='content_addressed_readback'),
      published_at timestamptz NOT NULL,available_at timestamptz NOT NULL,
      retrieved_at timestamptz NOT NULL,valid_at timestamptz NOT NULL,
      issued_at timestamptz,archived_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      archived_original boolean NOT NULL DEFAULT true,supersedes_id uuid,
      UNIQUE(tenant_id,snapshot_id),
      FOREIGN KEY(tenant_id,snapshot_id,content_digest,byte_count)
        REFERENCES research.dataset_snapshots(tenant_id,id,raw_content_digest,byte_count),
      CHECK(published_at<=available_at AND available_at<=retrieved_at),
      CHECK(valid_at<=retrieved_at),
      CHECK(issued_at IS NULL OR issued_at<=valid_at),
      CHECK(archived_original=true AND supersedes_id IS NULL))""")
    op.execute("ALTER TABLE research.historical_archive_entries ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE research.historical_archive_entries FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY historical_archive_entries_tenant
      ON research.historical_archive_entries
      USING(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)
      WITH CHECK(tenant_id=nullif(current_setting('orca.tenant_id',true),'')::uuid)""")
    op.execute("GRANT SELECT,INSERT ON research.historical_archive_entries TO orca_runtime")
    op.execute("""CREATE FUNCTION research.stamp_historical_archive()
      RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        NEW.archived_at=clock_timestamp();
        IF NEW.retrieved_at>NEW.archived_at THEN
          RAISE EXCEPTION 'archive cannot precede retrieval';
        END IF;
        RETURN NEW;
      END $$""")
    op.execute("""CREATE TRIGGER historical_archive_stamp
      BEFORE INSERT ON research.historical_archive_entries
      FOR EACH ROW EXECUTE FUNCTION research.stamp_historical_archive()""")
    op.execute("""CREATE TRIGGER historical_archive_entries_immutable
      BEFORE UPDATE OR DELETE ON research.historical_archive_entries
      FOR EACH ROW EXECUTE FUNCTION research.protect_tabular_evidence()""")
    op.execute("""CREATE INDEX historical_archive_source_cutoff
      ON research.historical_archive_entries(tenant_id,source_id,archived_at)""")


def downgrade() -> None:
    raise RuntimeError("Historical archive evidence requires explicit retention review")
