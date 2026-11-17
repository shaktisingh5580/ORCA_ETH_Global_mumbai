"""Purpose: prove P07 archival replay selects only exact bytes known by the decision cutoff.
Behavior: use a disposable local database and test-only source; test absence, access and corruption.
Refs: main_implementation plan.md P07.3; api design.md §3.8; Phase: P07.
"""

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from orca.application.acquisition_worker import AcquisitionResult, AcquisitionWorker
from orca.application.catalog import CatalogService
from orca.application.historical_replay import HistoricalReplay
from orca.application.snapshot_requests import SnapshotRequests
from orca.application.snapshots import SnapshotRepository
from orca.contracts.catalog import ProductVersion, ScientificProduct
from orca.contracts.snapshot_requests import SnapshotRequest, request_digest
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.storage.snapshot_files import SnapshotFiles


@pytest.mark.live
def test_archived_observation_replay_requires_cutoff_grant_and_exact_bytes(tmp_path: Path) -> None:
    admin_url = os.environ.get("ORCA_TEST_DATABASE_URL", "")
    runtime_url = os.environ.get("ORCA_TEST_RUNTIME_DATABASE_URL", "")
    if not admin_url or not runtime_url:
        pytest.skip("disposable local Postgres unavailable")
    if any(
        "@127.0.0.1:" not in url or not url.endswith("/orca_test")
        for url in (admin_url, runtime_url)
    ):
        pytest.fail("historical replay requires disposable localhost orca_test")
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
    tenant, org, project, source_id = uuid4(), uuid4(), uuid4(), uuid4()
    store = SnapshotFiles(tmp_path)
    raw = b"test-only preserved observation bytes\n"
    now = datetime.now(timezone.utc)
    try:
        with admin.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO iam.organizations(id,tenant_id,name,slug) VALUES(:id,:t,'replay',:s)"
                ),
                {"id": org, "t": tenant, "s": str(tenant)},
            )
            connection.execute(
                text(
                    "INSERT INTO research.projects(id,tenant_id,slug,name) VALUES(:id,:t,:s,'replay')"
                ),
                {"id": project, "t": tenant, "s": str(project)},
            )

        def transaction():
            return TenantUnitOfWork(runtime, tenant)

        version = ProductVersion(
            id=uuid4(),
            tenant_id=tenant,
            product_id=source_id,
            source_version="archived-1",
            metadata_digest="sha256:" + "a" * 64,
            native_variable="sst",
            standard_name="sea_surface_temperature",
            physical_definition="test-only observed SST",
            native_units="K",
            calendar="gregorian",
            published_at=now - timedelta(days=3),
            available_at=now - timedelta(days=2, hours=12),
            retrieved_at=now - timedelta(days=2),
            license={
                "license_ref": "test-license",
                "evidence_digest": "sha256:" + "b" * 64,
                "can_preserve": True,
            },
        )
        request = SnapshotRequest(
            organization_id=org,
            product_version_id=version.id,
            geo_scope={"type": "Point", "geometry": {"type": "Point", "coordinates": [68.5, 20.5]}},
            time_window={
                "start": now - timedelta(days=1, hours=12),
                "end": now - timedelta(days=1),
            },
            variables=["sst"],
        )
        with transaction() as session:
            catalog = CatalogService(session, tenant)
            catalog.add_product(
                ScientificProduct(
                    id=source_id,
                    tenant_id=tenant,
                    provider_id="test",
                    source_product_id="test-observation",
                    kind="observation",
                    status="validated",
                )
            )
            catalog.add_version(version)
            SnapshotRequests(session, tenant).create(request, "historical-replay")

        class Source:
            def fetch(self, body: SnapshotRequest, pinned: ProductVersion) -> AcquisitionResult:
                assert body == request and pinned == version
                return AcquisitionResult(
                    raw, "https://example.org/observation.nc", request_digest(body, pinned)
                )

        assert AcquisitionWorker(tenant, transaction, Source(), store).run_once() == "succeeded"
        with transaction() as session:
            snapshot = SnapshotRepository(session, tenant).list()[0]
            replay = HistoricalReplay(session, tenant, store)
            archived = session.execute(
                text(
                    "SELECT archived_at,object_version_id,retention_kind FROM research.historical_archive_entries WHERE tenant_id=:tenant AND snapshot_id=:snapshot"
                ),
                {"tenant": tenant, "snapshot": snapshot.id},
            ).one()
            assert archived.archived_at >= now
            assert archived.object_version_id == snapshot.raw_content_digest
            assert archived.retention_kind == "content_addressed_readback"
            assert (
                replay.select(
                    project_id=project, source_id=source_id, as_of=datetime.now(timezone.utc)
                ).status
                == "inconclusive"
            )
            SnapshotRepository(session, tenant).grant_project(snapshot.id, project)
        with transaction() as session:
            replay = HistoricalReplay(session, tenant, store)
            with pytest.raises(ValueError, match="timezone"):
                replay.select(project_id=project, source_id=source_id, as_of=datetime.now())
            selected = replay.select(
                project_id=project, source_id=source_id, as_of=datetime.now(timezone.utc)
            )
            assert selected.status == "selected" and selected.snapshot_id == snapshot.id
            assert (
                replay.select(
                    project_id=project, source_id=source_id, as_of=now - timedelta(seconds=1)
                ).status
                == "inconclusive"
            )
        (tmp_path / str(tenant) / snapshot.raw_content_digest[7:]).write_bytes(b"corrupted")
        with transaction() as session:
            assert (
                HistoricalReplay(session, tenant, store)
                .select(project_id=project, source_id=source_id, as_of=datetime.now(timezone.utc))
                .status
                == "blocked"
            )
    finally:
        runtime.dispose()
        admin.dispose()
