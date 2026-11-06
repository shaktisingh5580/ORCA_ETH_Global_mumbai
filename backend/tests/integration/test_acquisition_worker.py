"""Purpose: prove durable acquisition orchestration and atomic publication in PostgreSQL.
Behavior: inject source bytes only in tests; verify failures, recovery and stale fencing.
Refs: main implementation plan P03.3/P03.5; API 3.4; Phase: P03.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from orca.application.acquisition_worker import (
    AcquisitionFailed,
    AcquisitionResult,
    AcquisitionWorker,
)
from orca.application.catalog import CatalogService
from orca.application.snapshot_requests import SnapshotRequests
from orca.application.snapshots import SnapshotRepository
from orca.application.source_probes import SourceProbes
from orca.contracts.acquisition import SourceBinding
from orca.contracts.catalog import ProductVersion, ScientificProduct
from orca.contracts.snapshot_requests import SnapshotRequest, request_digest
from orca.contracts.source_probes import ProbeRequest
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.storage.snapshot_files import SnapshotFiles
from orca.probe_worker import run_once as run_probe


@pytest.fixture(scope="module")
def worker_schema():
    admin = os.environ.get("ORCA_TEST_DATABASE_URL", "")
    if not admin:
        pytest.skip("local disposable database required")
    if "@127.0.0.1:" not in admin or not admin.endswith("/orca_test"):
        pytest.fail("worker tests require local orca_test")
    backend = Path(__file__).resolve().parents[2]
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(backend / "alembic.ini"), "upgrade", "head"],
        cwd=backend,
        env=dict(os.environ, ORCA_DATABASE_URL=admin),
        check=True,
        capture_output=True,
        timeout=120,
    )


@pytest.mark.live
@pytest.mark.parametrize("outcome", ["success", "rejected", "mismatch", "crash", "expired"])
def test_acquisition_atomic_publication_and_recovery(tmp_path, outcome, worker_schema):
    admin = os.environ.get("ORCA_TEST_DATABASE_URL", "")
    runtime = os.environ.get("ORCA_TEST_RUNTIME_DATABASE_URL", "")
    if not admin or not runtime:
        pytest.skip("local disposable database required")
    if any("@127.0.0.1:" not in url or not url.endswith("/orca_test") for url in (admin, runtime)):
        pytest.fail("worker tests require local orca_test")
    owner = create_engine(admin.replace("postgresql://", "postgresql+psycopg://", 1))
    engine = create_engine(runtime.replace("postgresql://", "postgresql+psycopg://", 1))
    tenant, org, product = uuid4(), uuid4(), uuid4()

    def transaction():
        return TenantUnitOfWork(engine, tenant)

    try:
        with owner.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO iam.organizations(id,tenant_id,name,slug) VALUES(:id,:tenant,'worker-test',:slug)"
                ),
                {"id": org, "tenant": tenant, "slug": str(tenant)},
            )
        version = ProductVersion(
            id=uuid4(),
            tenant_id=tenant,
            product_id=product,
            source_version="test",
            metadata_digest="sha256:" + "a" * 64,
            native_variable="sst",
            standard_name="sea_surface_temperature",
            physical_definition="test-only SST",
            native_units="K",
            calendar="gregorian",
            retrieved_at="2025-02-01T00:00:00Z",
            license={
                "license_ref": "test",
                "evidence_digest": "sha256:" + "b" * 64,
                "can_preserve": True,
            },
        )
        body = SnapshotRequest(
            organization_id=org,
            product_version_id=version.id,
            geo_scope={"type": "Point", "geometry": {"type": "Point", "coordinates": [68.5, 20.5]}},
            time_window={"start": "2025-01-01T00:00:00Z", "end": "2025-01-02T00:00:00Z"},
            variables=["sst"],
        )
        with transaction() as session:
            catalog = CatalogService(session, tenant)
            catalog.add_product(
                ScientificProduct(
                    id=product,
                    tenant_id=tenant,
                    provider_id="test",
                    source_product_id="test",
                    kind="observation",
                )
            )
            catalog.add_version(version)
            queued = SnapshotRequests(session, tenant).create(body, "worker")

        class Source:
            calls = 0

            def fetch(self, request, pinned):
                self.calls += 1
                assert request == body and pinned == version
                # A separate transaction sees the committed lease during network I/O.
                with transaction() as session:
                    assert SnapshotRequests(session, tenant).claim() is None
                    assert (
                        SnapshotRequests(session, tenant).get(queued.operation.id).operation.status
                        == "running"
                    )
                    if outcome == "expired":
                        session.execute(
                            text(
                                "UPDATE research.snapshot_requests SET lease_until=clock_timestamp()-interval '1 second' WHERE tenant_id=:tenant"
                            ),
                            {"tenant": tenant},
                        )
                        assert SnapshotRequests(session, tenant).claim()["attempts"] == 2
                if outcome == "rejected":
                    raise AcquisitionFailed("private provider details")
                if outcome == "crash" and self.calls == 1:
                    raise OSError("temporary storage or network interruption")
                digest = "sha256:" + "0" * 64 if outcome == "mismatch" else request_digest(request)
                return AcquisitionResult(
                    b"test-only exact bytes\r\n", "https://example.org/test.nc", digest
                )

        source = Source()
        worker = AcquisitionWorker(tenant, transaction, source, SnapshotFiles(tmp_path))
        if outcome == "crash":
            with pytest.raises(OSError):
                worker.run_once()
            with transaction() as session:
                assert SnapshotRepository(session, tenant).list() == []
                assert (
                    SnapshotRequests(session, tenant).get(queued.operation.id).operation.status
                    == "running"
                )
                session.execute(
                    text(
                        "UPDATE research.snapshot_requests SET lease_until=clock_timestamp()-interval '1 second' WHERE tenant_id=:tenant"
                    ),
                    {"tenant": tenant},
                )
            assert worker.run_once() == "succeeded"
        else:
            assert (
                worker.run_once()
                == {
                    "success": "succeeded",
                    "rejected": "failed",
                    "mismatch": "failed",
                    "expired": "lease_lost",
                }[outcome]
            )
        with transaction() as session:
            snapshots = SnapshotRepository(session, tenant).list()
            status = SnapshotRequests(session, tenant).get(queued.operation.id)
            if outcome in {"success", "crash"}:
                assert len(snapshots) == 1 and status.snapshot_id == snapshots[0].id
                assert snapshots[0].acquisition_request_digest == request_digest(body)
                assert snapshots[0].scientific_status == "not_validated"
                assert status.attempts == (2 if outcome == "crash" else 1)
            else:
                assert snapshots == [] and status.snapshot_id is None
                assert status.error_code == (
                    None if outcome == "expired" else "ACQUISITION_REJECTED"
                )
        if outcome != "expired":
            assert worker.run_once() == "idle"
        with transaction() as session:
            probe = SourceProbes(session, tenant).create(
                ProbeRequest(
                    organization_id=org,
                    provider_id="test",
                    distribution_id=version.id,
                    region=body.geo_scope,
                    time_window=body.time_window,
                ),
                "probe",
            )
        metadata = json.dumps(
            {
                "table": {
                    "columnNames": [
                        "Row Type",
                        "Variable Name",
                        "Attribute Name",
                        "Data Type",
                        "Value",
                    ],
                    "rows": [["variable", "sst", "", "float", "time, latitude, longitude"]],
                }
            }
        ).encode()
        binding = SourceBinding(
            tenant_id=tenant, version_id=version.id, host="coastwatch.pfeg.noaa.gov", dataset="test"
        )
        with patch("orca.probe_worker.ERDDAP._get", return_value=metadata):
            assert run_probe(tenant, transaction, (binding,)) == "succeeded"
        with transaction() as session:
            result = SourceProbes(session, tenant).get(probe.operation.id)
            assert result.evidence.response_bytes == len(metadata)
            assert not result.evidence.metadata_matches_pinned_version
            assert result.evidence.scientific_status == "not_validated"
        assert run_probe(tenant, transaction, (binding,)) == "idle"
    finally:
        engine.dispose()
        owner.dispose()
