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
from orca.application.snapshots import SnapshotRepository
from orca.contracts.catalog import ProductVersion, ScientificProduct
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
                    second = manifest.model_copy(update={"id": uuid4()})
                    repository.add(second)
                    ordered = sorted([manifest, second], key=lambda item: item.id)
                    assert repository.list(limit=1) == ordered[:1]
                    assert repository.list(after=ordered[0].id, limit=1) == ordered[1:]
                    assert repository.list(after=ordered[1].id) == []
                    assert (
                        len(
                            repository.list(
                                product_id=product_id, digest=manifest.raw_content_digest
                            )
                        )
                        == 2
                    )
                    assert repository.list(product_id=uuid4()) == []
                    assert repository.list(digest="sha256:" + "0" * 64) == []
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
                    assert repository.list() == []
                    session.execute(text("RESET ROLE"))
                    with pytest.raises(DBAPIError), session.begin_nested():
                        session.execute(text("UPDATE research.dataset_snapshots SET byte_count=5"))
                    session.rollback()
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
