"""Purpose: exercise P14 operator handover against real failures and isolated PostgreSQL.
Behavior: verify routing, durable quota rejection, downgrade refusal and private export expiry.
Refs: main_implementation plan.md P14.5; api design.md §§14,20. Phase: P14.
"""

import importlib.util
import io
import os
import subprocess
import sys
import zipfile
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException
from p05_queue import approved_queue, queue_command
from p06_evidence import insufficient_manifest
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy import text

from orca.api.routers import evidence
from orca.api.routers.investigations import raise_service_error
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.jobs.postgres_queue import PostgresQueue, RunRateLimitExceeded

BACKEND = Path(__file__).resolve().parents[2]


def test_alert_routes_reach_owner():
    spec = importlib.util.spec_from_file_location(
        "health_alert", BACKEND.parent / "infra/observability/health_alert.py"
    )
    assert spec is not None and spec.loader is not None
    monitor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(monitor)
    sent = []
    receiver = "https://receiver.invalid/orca"
    ack = 202

    def transport(url, method, headers, body=None):
        if method == "GET":
            if url.endswith("/livez"):
                assert not headers
                return 200
            assert headers == {"Authorization": "Bearer local-test-token"}
            return 503
        assert url == receiver
        assert headers == {"Content-Type": "application/json"}
        assert (
            body == b'{"alert": "orca_readiness_failed", "owner": "platform", "http_status": 503}'
        )
        sent.append(body)
        return ack

    def run():
        return monitor.check(
            "https://orca.invalid", "local-test-token", {"platform": receiver}, transport
        )

    assert run()[0]["delivered"] is True
    ack = 302
    assert run()[0]["delivered"] is False
    assert len(sent) == 2
    with pytest.raises(ValueError, match="HTTPS"):
        monitor.check(
            "https://orca.invalid",
            "local-test-token",
            {"platform": "http://receiver.invalid"},
            transport,
        )
    assert len(sent) == 2


def isolated_database():
    urls = [
        os.environ.get(name)
        for name in ("ORCA_TEST_DATABASE_URL", "ORCA_TEST_RUNTIME_DATABASE_URL")
    ]
    if not all(urls):
        pytest.skip("Configure a disposable P14.5 loopback PostgreSQL database")
    for url in urls:
        assert url is not None
        conn = conninfo_to_dict(url)
        assert conn.get("host") == "127.0.0.1" and conn.get("port")
        assert conn["dbname"].startswith("orca_p145_") and conn["dbname"].endswith("_test")
    return urls[0]


@pytest.mark.live
@pytest.mark.enable_socket
def test_quota_exceeded_actions():
    isolated_database()
    database, tenant, project, investigation, approved = approved_queue()
    try:

        def command(key):
            return queue_command(
                investigation, approved.plan_id, approved.approved_digest, key, "0.10"
            )

        with TenantUnitOfWork(database, tenant) as session:
            session.execute(
                text(
                    "INSERT INTO iam.organization_run_quotas"
                    " (tenant_id,max_runs_per_minute) VALUES (:tenant,1)"
                    " ON CONFLICT (tenant_id) DO UPDATE SET max_runs_per_minute=1"
                ),
                {"tenant": tenant},
            )
            accepted = PostgresQueue(session, tenant, project).enqueue_run(command("first"))
        with pytest.raises(RunRateLimitExceeded) as limited:
            with TenantUnitOfWork(database, tenant) as session:
                PostgresQueue(session, tenant, project).enqueue_run(command("second"))
        with pytest.raises(HTTPException) as response:
            raise_service_error(limited.value)
        assert response.value.status_code == 429
        assert response.value.headers == {"Retry-After": "60"}
        with TenantUnitOfWork(database, tenant) as session:
            assert PostgresQueue(session, tenant, project).enqueue_run(command("first")) == accepted
            for table in ("compute_runs", "run_budget_reservations", "job_queue"):
                assert (
                    session.execute(text(f"SELECT count(*) FROM research.{table}")).scalar_one()
                    == 1
                )
    finally:
        database.dispose()


@pytest.mark.live
@pytest.mark.enable_socket
def test_staging_rollback_migration_safe():
    url = isolated_database()
    assert url is not None
    env = dict(os.environ, ORCA_DATABASE_URL=url)

    def migration(*args):
        return subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
            cwd=BACKEND,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )

    assert migration("upgrade", "head").returncode == 0
    with psycopg.connect(url, autocommit=True) as connection:
        before = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert before == ("0043_identity_binding_challenges",)
        refused = migration("downgrade", "-1")
        assert refused.returncode != 0
        assert "Identity binding evidence requires explicit recovery review" in refused.stderr
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == before
        assert connection.execute(
            "SELECT to_regclass('federation.identity_binding_challenges') IS NOT NULL"
        ).fetchone() == (True,)


def test_privacy_export_retention(monkeypatch):
    manifest = insufficient_manifest()
    artifact = uuid4()
    expired = False

    class Session:
        def execute(self, statement, params):
            assert "expires_at>clock_timestamp()" in str(statement)
            assert params["project"] == manifest.project_id
            return SimpleNamespace(scalar_one_or_none=lambda: None if expired else 1)

    def snapshot(identifier):
        source = next(item for item in manifest.inputs if item.snapshot_id == identifier)
        return SimpleNamespace(
            raw_content_digest=source.raw_content_digest,
            byte_count=source.byte_count,
            product_version=SimpleNamespace(license=SimpleNamespace(license_ref="test-license")),
        )

    monkeypatch.setattr(evidence, "engine", lambda _: None)
    monkeypatch.setattr(evidence, "_artifact_context", lambda *_: manifest.tenant_id)
    monkeypatch.setattr(evidence, "_authorize", lambda *_: None)
    monkeypatch.setattr(evidence, "TenantUnitOfWork", lambda *_: nullcontext(Session()))
    monkeypatch.setattr(evidence, "SnapshotRepository", lambda *_: SimpleNamespace(get=snapshot))
    monkeypatch.setattr(
        evidence,
        "_artifact_row",
        lambda *_: {
            "id": artifact,
            "project_id": manifest.project_id,
            "investigation_id": uuid4(),
            "run_id": uuid4(),
            "evidence_manifest_id": manifest.id,
            "scientific_status": manifest.result_status,
            "created_at": manifest.created_at,
            "manifest": manifest.model_dump(mode="json"),
            "manifest_digest": manifest.manifest_digest,
        },
    )
    response = evidence.download_reference_export(artifact, None, None)
    assert response.headers["cache-control"] == "private, no-store"
    with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
        assert archive.namelist() == ["manifest.json"]
    expired = True
    with pytest.raises(HTTPException) as denied:
        evidence.download_reference_export(artifact, None, None)
    assert denied.value.status_code == 403
