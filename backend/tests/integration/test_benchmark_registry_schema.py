"""Purpose: verify P07 benchmark definitions remain immutable and tenant isolated.
Behavior: inspect the migrated disposable database and runtime grants without benchmark answers.
Refs: main_implementation plan.md P07.4; api design.md §3.8; Phase: P07.
"""

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest


@pytest.mark.live
def test_benchmark_registry_is_frozen_and_tenant_guarded() -> None:
    url = os.environ.get("ORCA_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("disposable local Postgres unavailable")
    if "@127.0.0.1:" not in url or not url.endswith("/orca_test"):
        pytest.fail("benchmark schema check requires disposable localhost orca_test")
    backend = Path(__file__).resolve().parents[2]
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(backend / "alembic.ini"), "upgrade", "head"],
        cwd=backend,
        env=dict(os.environ, ORCA_DATABASE_URL=url),
        capture_output=True,
        check=True,
        timeout=120,
    )
    with psycopg.connect(url) as connection:
        security = connection.execute(
            """SELECT c.relrowsecurity,c.relforcerowsecurity,
              has_table_privilege('orca_runtime',c.oid,'SELECT'),
              has_table_privilege('orca_runtime',c.oid,'INSERT'),
              has_table_privilege('orca_runtime',c.oid,'UPDATE'),
              has_table_privilege('orca_runtime',c.oid,'DELETE')
              FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
              WHERE n.nspname='research' AND c.relname='benchmark_definitions'"""
        ).fetchone()
        assert security == (True, True, True, True, False, False)
        triggers = connection.execute(
            """SELECT tgname FROM pg_trigger
              WHERE tgrelid='research.benchmark_definitions'::regclass AND NOT tgisinternal"""
        ).fetchall()
        assert ("benchmark_definitions_immutable",) in triggers
        columns = {
            row[0]
            for row in connection.execute(
                """SELECT column_name FROM information_schema.columns
                  WHERE table_schema='research' AND table_name='benchmark_definitions'"""
            ).fetchall()
        }
        assert "held_out_answers" not in columns
        assert {"frozen_test_set_digest", "reference_policy_digest", "synthetic"} <= columns
