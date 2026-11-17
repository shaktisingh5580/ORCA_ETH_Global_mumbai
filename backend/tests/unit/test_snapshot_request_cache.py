"""Purpose: verify P03 scoped snapshot-cache lookup without infrastructure.
Behavior: reuse only a tenant-bound snapshot matching the full acquisition request digest.
Refs: API 3.4; main implementation plan P03.5; P03-snapshot-read-contract.json; Phase: P03.5.
"""

from datetime import datetime, timezone
from uuid import uuid4

from orca.application.snapshot_requests import SnapshotRequests
from orca.contracts.catalog import ProductVersion
from orca.contracts.snapshot_requests import SnapshotRequest, request_digest


class _Result:
    def __init__(self, scalar=None, mapping=None):
        self.scalar = scalar
        self.mapping = mapping

    def scalar_one_or_none(self):
        return self.scalar

    def mappings(self):
        return self

    def one_or_none(self):
        return self.mapping


class _CacheSession:
    def __init__(self, version: ProductVersion, snapshot_id=None):
        self.version = version
        self.snapshot_id = snapshot_id
        self.calls = []
        self.request_id = uuid4()

    def execute(self, statement, params):
        sql = str(statement)
        self.calls.append((sql, params))
        if "SELECT manifest FROM research.catalog_versions" in sql:
            return _Result(scalar=self.version.model_dump(mode="json"))
        if "SELECT id FROM research.dataset_snapshots" in sql:
            assert "tenant_id=:tenant" in sql
            assert "manifest->>'acquisition_request_digest'=:digest" in sql
            return _Result(scalar=self.snapshot_id)
        if "INSERT INTO research.snapshot_requests" in sql:
            self.insert_params = params
            return _Result(scalar=self.request_id)
        if "SELECT * FROM research.snapshot_requests" in sql:
            return _Result(
                mapping={
                    "id": self.request_id,
                    "status": "succeeded" if self.snapshot_id else "queued",
                    "snapshot_id": self.snapshot_id,
                    "error_code": None,
                    "attempts": 0,
                    "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                    "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                }
            )
        raise AssertionError(f"unexpected SQL: {sql}")


def _inputs():
    tenant, org, product, version_id = uuid4(), uuid4(), uuid4(), uuid4()
    version = ProductVersion(
        id=version_id,
        tenant_id=tenant,
        product_id=product,
        source_version="cycle-2025-01",
        metadata_digest="sha256:" + "a" * 64,
        native_variable="sst",
        standard_name="sea_surface_temperature",
        physical_definition="Sea surface temperature",
        native_units="degree_Celsius",
        calendar="gregorian",
        retrieved_at="2025-01-31T00:00:00Z",
        available_at="2025-01-30T00:00:00Z",
        license={
            "license_ref": "license-v1",
            "evidence_digest": "sha256:" + "b" * 64,
            "can_preserve": True,
        },
    )
    request = SnapshotRequest(
        organization_id=org,
        product_version_id=version_id,
        geo_scope={
            "type": "Point",
            "geometry": {"type": "Point", "coordinates": [68.5, 20.5]},
        },
        time_window={
            "start": "2025-01-01T00:00:00Z",
            "end": "2025-01-30T00:00:00Z",
        },
        variables=["sst"],
        as_of="2025-01-31T00:00:00Z",
    )
    return tenant, version, request


def test_snapshot_cache_hit_is_bound_to_tenant_and_exact_scope():
    tenant, version, request = _inputs()
    snapshot_id = uuid4()
    session = _CacheSession(version, snapshot_id)

    result = SnapshotRequests(session, tenant).create(request, "cache-hit")

    assert result.operation.status == "succeeded"
    assert result.snapshot_id == snapshot_id
    lookup_sql, lookup_params = session.calls[1]
    assert "tenant_id=:tenant" in lookup_sql
    assert lookup_params["tenant"] == tenant
    assert lookup_params["digest"] == request_digest(request, version)
    assert session.insert_params["status"] == "succeeded"
    assert session.insert_params["snapshot"] == snapshot_id


def test_snapshot_cache_miss_queues_work():
    tenant, version, request = _inputs()
    session = _CacheSession(version)

    result = SnapshotRequests(session, tenant).create(request, "cache-miss")

    assert result.operation.status == "queued"
    assert result.snapshot_id is None
    assert session.insert_params["status"] == "queued"
