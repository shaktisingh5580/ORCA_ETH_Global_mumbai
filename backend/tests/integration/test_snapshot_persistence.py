"""Purpose: prove snapshot persistence, tenant isolation and immutability in real PostgreSQL.
Behavior: seed only a local disposable database and roll back all test records.
Refs: main plan P03.3; research-only guide 4.5; Phase: P03.3.
"""

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from orca.application.catalog import CatalogService
from orca.application.projects import IdempotencyConflict
from orca.application.snapshot_requests import SnapshotRequests
from orca.application.snapshots import SnapshotRepository
from orca.contracts.catalog import ProductVersion, ScientificProduct
from orca.contracts.snapshot_requests import SnapshotRequest, request_digest
from orca.contracts.snapshots import DatasetSnapshotManifest


@pytest.mark.live
def test_snapshot_persistence_isolation_and_immutability():
    url = os.environ.get("ORCA_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("local live database unavailable")
    if "@127.0.0.1:" not in url or "/orca_test" not in url:
        pytest.fail("snapshot tests require isolated local orca_test")
    engine = create_engine(url.replace("postgresql://", "postgresql+psycopg://", 1))
    tenant, other, product_id = uuid4(), uuid4(), uuid4()
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(
                    text(
                        "INSERT INTO iam.organizations(id,tenant_id,name,slug) VALUES(:id,:tenant,'test',:slug)"
                    ),
                    {"id": uuid4(), "tenant": tenant, "slug": str(tenant)},
                )
                connection.execute(text("SET LOCAL ROLE orca_runtime"))
                with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                    session.execute(
                        text("SELECT set_config('orca.tenant_id',:tenant,true)"),
                        {"tenant": str(tenant)},
                    )
                    catalog = CatalogService(session, tenant)
                    catalog.add_product(
                        ScientificProduct(
                            id=product_id,
                            tenant_id=tenant,
                            provider_id="test",
                            source_product_id="test",
                            kind="observation",
                        )
                    )
                    version = ProductVersion(
                        id=uuid4(),
                        tenant_id=tenant,
                        product_id=product_id,
                        source_version="v1",
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
                    catalog.add_version(version)
                    manifest = DatasetSnapshotManifest(
                        id=uuid4(),
                        tenant_id=tenant,
                        product_version=version,
                        raw_content_digest="sha256:" + "c" * 64,
                        byte_count=4,
                        origin_uri="https://example.org/test",
                        retrieved_at=datetime.now(timezone.utc),
                    )
                    repository = SnapshotRepository(session, tenant)
                    repository.add(manifest)
                    assert repository.get(manifest.id) == manifest
                    body = SnapshotRequest(
                        organization_id=uuid4(),
                        product_version_id=version.id,
                        geo_scope={
                            "type": "Point",
                            "geometry": {"type": "Point", "coordinates": [68.5, 20.5]},
                        },
                        time_window={
                            "start": "2025-01-01T00:00:00Z",
                            "end": "2025-02-01T00:00:00Z",
                        },
                        variables=["sst"],
                    )
                    requests = SnapshotRequests(session, tenant)
                    queued = requests.create(body, "test-key")
                    assert requests.create(body, "test-key").operation.id == queued.operation.id
                    with pytest.raises(IdempotencyConflict):
                        requests.create(
                            body.model_copy(update={"organization_id": uuid4()}), "test-key"
                        )
                    claim = requests.claim()
                    assert claim is not None and requests.claim() is None
                    assert not requests.complete(queued.operation.id, uuid4(), manifest.id)
                    session.execute(
                        text(
                            "UPDATE research.snapshot_requests SET lease_until=clock_timestamp()-interval '1 second'"
                        )
                    )
                    recovered = requests.claim()
                    assert recovered is not None and recovered["attempts"] == 2
                    assert not requests.complete(
                        queued.operation.id, recovered["lease_token"], manifest.id
                    )
                    bound = manifest.model_copy(
                        update={"id": uuid4(), "acquisition_request_digest": request_digest(body)}
                    )
                    repository.add(bound)
                    assert not requests.complete(
                        queued.operation.id, claim["lease_token"], manifest.id
                    )
                    assert requests.complete(
                        queued.operation.id, recovered["lease_token"], bound.id
                    )
                    assert not requests.fail(
                        queued.operation.id, recovered["lease_token"], "SOURCE_TIMEOUT"
                    )
                    assert requests.get(queued.operation.id).snapshot_id == bound.id
                    second = manifest.model_copy(update={"id": uuid4()})
                    repository.add(second)
                    ordered = sorted([manifest, second, bound], key=lambda item: item.id)
                    assert repository.list(limit=1) == ordered[:1]
                    assert repository.list(after=ordered[0].id, limit=1) == ordered[1:2]
                    assert repository.list(after=ordered[2].id) == []
                    assert (
                        len(
                            repository.list(
                                product_id=product_id, digest=manifest.raw_content_digest
                            )
                        )
                        == 3
                    )
                    assert repository.list(product_id=uuid4()) == []
                    assert repository.list(digest="sha256:" + "0" * 64) == []
                    project, principal = uuid4(), uuid4()
                    session.execute(
                        text(
                            "INSERT INTO research.projects(id,tenant_id,slug,name) VALUES(:id,:tenant,:slug,'test')"
                        ),
                        {"id": project, "tenant": tenant, "slug": str(project)},
                    )
                    assert not repository.project_member(project, str(principal))
                    session.execute(
                        text(
                            "INSERT INTO research.project_members(id,tenant_id,project_id,principal_id,role) VALUES(:id,:tenant,:project,:principal,'researcher')"
                        ),
                        {
                            "id": uuid4(),
                            "tenant": tenant,
                            "project": project,
                            "principal": principal,
                        },
                    )
                    assert repository.project_member(project, str(principal))
                    assert repository.list(project_id=project) == []
                    repository.grant_project(manifest.id, project)
                    assert repository.list(project_id=project) == [manifest]
                    repository.grant_project(
                        manifest.id, project, datetime(2020, 1, 1, tzinfo=timezone.utc)
                    )
                    assert repository.list(project_id=project) == []
                    repository.grant_project(manifest.id, project)
                    repository.revoke_project(manifest.id, project)
                    assert repository.list(project_id=project) == []
                    with pytest.raises(DBAPIError), session.begin_nested():
                        repository.grant_project(manifest.id, uuid4())
                    with pytest.raises(ValueError):
                        repository.add(
                            manifest.model_copy(
                                update={
                                    "id": uuid4(),
                                    "product_version": version.model_copy(
                                        update={"source_version": "forged"}
                                    ),
                                }
                            )
                        )
                    with pytest.raises(DBAPIError), session.begin_nested():
                        session.execute(text("DELETE FROM research.dataset_snapshots"))
                    session.execute(
                        text("SELECT set_config('orca.tenant_id',:tenant,true)"),
                        {"tenant": str(other)},
                    )
                    assert SnapshotRepository(session, other).get(manifest.id) is None
                    assert repository.get(manifest.id) is None
                    assert SnapshotRequests(session, other).get(queued.operation.id) is None
                    assert repository.list() == []
                    session.execute(text("RESET ROLE"))
                    with pytest.raises(DBAPIError), session.begin_nested():
                        session.execute(text("UPDATE research.dataset_snapshots SET byte_count=5"))
                    session.rollback()
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
