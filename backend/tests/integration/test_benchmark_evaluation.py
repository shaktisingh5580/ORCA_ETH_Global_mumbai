"""Purpose: verify P07 frozen benchmark scoring, absent truth and corrupt truth on local Postgres.
Behavior: use test-only private reference bytes and inspect durable immutable outcomes.
Refs: main_implementation plan.md P07.4; api design.md §4.5; Phase: P07.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from orca.application.benchmark_evaluation import BenchmarkEvaluator
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.storage.snapshot_files import SnapshotFiles


@pytest.mark.live
def test_frozen_reference_scoring_is_durable_and_fails_closed(tmp_path: Path) -> None:
    admin_url = os.environ.get("ORCA_TEST_DATABASE_URL", "")
    runtime_url = os.environ.get("ORCA_TEST_RUNTIME_DATABASE_URL", "")
    if not admin_url or not runtime_url:
        pytest.skip("disposable local Postgres unavailable")
    if any(
        "@127.0.0.1:" not in url or not url.endswith("/orca_test")
        for url in (admin_url, runtime_url)
    ):
        pytest.fail("benchmark evaluation requires disposable localhost orca_test")
    backend = Path(__file__).resolve().parents[2]
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(backend / "alembic.ini"), "upgrade", "head"],
        cwd=backend,
        env=dict(os.environ, ORCA_DATABASE_URL=admin_url),
        capture_output=True,
        check=True,
        timeout=120,
    )
    admin = create_engine(admin_url.replace("postgresql://", "postgresql+psycopg://", 1))
    runtime = create_engine(runtime_url.replace("postgresql://", "postgresql+psycopg://", 1))
    tenant, org, project, benchmark, absent_benchmark = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    store = SnapshotFiles(tmp_path)
    raw = json.dumps(
        {
            "schema": "orca.benchmark.reference.v1",
            "samples": [{"id": "a", "observed": 1.0}, {"id": "b", "observed": 2.0}],
        },
        separators=(",", ":"),
    ).encode()
    digest = store.preserve(tenant, raw, permitted=True)
    try:
        with admin.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO iam.organizations(id,tenant_id,name,slug) VALUES(:id,:t,'benchmark',:s)"
                ),
                {"id": org, "t": tenant, "s": str(tenant)},
            )
            connection.execute(
                text(
                    "INSERT INTO research.projects(id,tenant_id,slug,name) VALUES(:id,:t,:s,'benchmark')"
                ),
                {"id": project, "t": tenant, "s": str(project)},
            )
            connection.execute(
                text("""INSERT INTO research.benchmark_definitions
                  (id,tenant_id,project_id,title,domain,method_id,version,
                   frozen_test_set_digest,reference_policy_digest,reference_kind,synthetic)
                  VALUES(:id,:tenant,:project,'Test observed SST','ocean','sst_mean','1',
                   :digest,:policy,'independent_observation',false)"""),
                {
                    "id": benchmark,
                    "tenant": tenant,
                    "project": project,
                    "digest": digest,
                    "policy": "sha256:" + "a" * 64,
                },
            )
            connection.execute(
                text("""INSERT INTO research.benchmark_definitions
                  (id,tenant_id,project_id,title,domain,method_id,version,
                   frozen_test_set_digest,reference_policy_digest,reference_kind,synthetic)
                  VALUES(:id,:tenant,:project,'Absent reference','ocean','sst_mean','2',
                   :digest,:policy,'synthetic_fixture',true)"""),
                {
                    "id": absent_benchmark,
                    "tenant": tenant,
                    "project": project,
                    "digest": "sha256:" + "c" * 64,
                    "policy": "sha256:" + "a" * 64,
                },
            )
        with TenantUnitOfWork(runtime, tenant) as session:
            evaluator = BenchmarkEvaluator(session, tenant, store)
            result = evaluator.evaluate(
                project_id=project, benchmark_id=benchmark, predictions={"a": 2.0, "b": 3.0}
            )
            assert result.status == "evaluated" and result.mae == pytest.approx(1.0)
            assert result.reference_independence_verified is False
            assert not result.synthetic
            assert (
                evaluator.evaluate(
                    project_id=project, benchmark_id=benchmark, predictions={"a": 2.0}
                ).reason
                == "SAMPLE_ALIGNMENT_MISMATCH"
            )
            missing = evaluator.evaluate(
                project_id=project,
                benchmark_id=absent_benchmark,
                predictions={"a": 2.0, "b": 3.0},
            )
            assert missing.status == "not_evaluable"
            assert missing.reason == "FROZEN_REFERENCE_UNAVAILABLE"
            assert missing.synthetic
        (tmp_path / str(tenant) / digest[7:]).write_bytes(b"corrupt")
        with TenantUnitOfWork(runtime, tenant) as session:
            blocked = BenchmarkEvaluator(session, tenant, store).evaluate(
                project_id=project, benchmark_id=benchmark, predictions={"a": 2.0, "b": 3.0}
            )
            assert blocked.status == "blocked"
            assert blocked.reason == "FROZEN_REFERENCE_CORRUPT"
            records = session.execute(
                text("""SELECT status,sample_count,reference_independence_verified
                  FROM research.benchmark_evaluations
                  WHERE tenant_id=:tenant AND project_id=:project AND benchmark_id=:benchmark
                  ORDER BY created_at,id"""),
                {"tenant": tenant, "project": project, "benchmark": benchmark},
            ).all()
            assert len(records) == 3
            assert {record.status for record in records} == {
                "evaluated",
                "not_evaluable",
                "blocked",
            }
            assert all(record.reference_independence_verified is False for record in records)
        with admin.connect() as connection:
            security = connection.execute(
                text("""SELECT c.relrowsecurity,c.relforcerowsecurity,
                  has_table_privilege('orca_runtime',c.oid,'SELECT'),
                  has_table_privilege('orca_runtime',c.oid,'INSERT'),
                  has_table_privilege('orca_runtime',c.oid,'UPDATE'),
                  has_table_privilege('orca_runtime',c.oid,'DELETE')
                  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                  WHERE n.nspname='research' AND c.relname='benchmark_evaluations'""")
            ).one()
            assert tuple(security) == (True, True, True, True, False, False)
            triggers = connection.execute(
                text("""SELECT tgname FROM pg_trigger
                  WHERE tgrelid='research.benchmark_evaluations'::regclass AND NOT tgisinternal""")
            ).all()
            assert ("benchmark_evaluations_immutable",) in triggers
            pinned = connection.execute(
                text("""SELECT count(*) FROM pg_constraint
                  WHERE conrelid='research.benchmark_evaluations'::regclass
                    AND conname='benchmark_evaluations_reference_pin'""")
            ).scalar_one()
            assert pinned == 1
    finally:
        runtime.dispose()
        admin.dispose()
