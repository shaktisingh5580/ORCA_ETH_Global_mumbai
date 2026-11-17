"""Purpose: verify P07 adjudications are append-only and tenant guarded on local Postgres.
Behavior: inspect the migrated disposable database and runtime privileges without touching research rows.
Refs: main_implementation plan.md P07.2; api design.md §3.8; Phase: P07.
"""

import os

import psycopg
import pytest


@pytest.mark.live
def test_resolution_storage_is_tenant_guarded_and_append_only() -> None:
    url = os.environ.get("ORCA_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("disposable local Postgres unavailable")
    if "@127.0.0.1:" not in url or "/orca_test" not in url:
        pytest.fail("P07 schema acceptance requires disposable localhost orca_test")
    with psycopg.connect(url) as connection:
        security = connection.execute(
            """SELECT c.relrowsecurity,c.relforcerowsecurity,
              has_table_privilege('orca_runtime',c.oid,'SELECT'),
              has_table_privilege('orca_runtime',c.oid,'INSERT'),
              has_table_privilege('orca_runtime',c.oid,'UPDATE'),
              has_table_privilege('orca_runtime',c.oid,'DELETE')
              FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
              WHERE n.nspname='research' AND c.relname='challenge_resolutions'"""
        ).fetchone()
        assert security == (True, True, True, True, False, False)
        triggers = connection.execute(
            """SELECT tgname FROM pg_trigger
              WHERE tgrelid='research.challenge_resolutions'::regclass
                AND NOT tgisinternal"""
        ).fetchall()
        assert ("challenge_resolutions_immutable",) in triggers
        unique = connection.execute(
            """SELECT count(*) FROM pg_constraint
              WHERE conrelid='research.challenge_resolutions'::regclass
                AND contype='u' AND array_length(conkey,1)=3"""
        ).fetchone()
        assert unique is not None and unique[0] >= 1
