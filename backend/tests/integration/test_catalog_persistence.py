"""Purpose: verify actual P03 catalog RLS, immutable versions and tenant foreign keys.
Behavior: use rollback-only local database transactions; never connect to cloud fixtures.
Refs: main plan P03.1; research-only guide 4.5; Phase: P03.1.
"""

import os
from datetime import datetime, timezone
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from orca.application.catalog import CatalogService
from orca.contracts.catalog import ProductVersion, ScientificProduct


@pytest.mark.live
def test_catalog_database_isolation_and_immutability():
    url = os.environ.get("ORCA_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("local live catalog database unavailable")
    if "@127.0.0.1:" not in url or "/orca_test" not in url:
        pytest.fail("catalog test requires isolated local orca_test database")
    tenant, outsider, product = uuid4(), uuid4(), uuid4()
    with psycopg.connect(url) as conn:
        try:
            conn.execute(
                "INSERT INTO iam.organizations(id,tenant_id,name,slug) VALUES(%s,%s,'catalog test',%s)",
                (uuid4(), tenant, str(tenant)),
            )
            conn.execute("SELECT set_config('orca.tenant_id',%s,true)", (str(tenant),))
            conn.execute("SET LOCAL ROLE orca_runtime")
            conn.execute(
                """INSERT INTO research.catalog_products
              (id,tenant_id,provider_id,source_product_id,kind) VALUES(%s,%s,'test','test','observation')""",
                (product, tenant),
            )
            conn.execute(
                """INSERT INTO research.catalog_versions
              (id,tenant_id,product_id,source_version,metadata_digest,manifest)
              VALUES(%s,%s,%s,'v1',%s,'{}')""",
                (uuid4(), tenant, product, "sha256:" + "a" * 64),
            )
            conn.execute("SELECT set_config('orca.tenant_id',%s,true)", (str(outsider),))
            assert conn.execute("SELECT count(*) FROM research.catalog_products").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM research.catalog_versions").fetchone()[0] == 0
            conn.execute("SELECT set_config('orca.tenant_id',%s,true)", (str(tenant),))
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute("UPDATE research.catalog_versions SET source_version='changed'")
            conn.execute("RESET ROLE")
            with pytest.raises(psycopg.errors.RaiseException), conn.transaction():
                conn.execute("UPDATE research.catalog_versions SET source_version='changed'")
            with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
                conn.execute(
                    """INSERT INTO research.catalog_versions
                  (id,tenant_id,product_id,source_version,metadata_digest,manifest)
                  VALUES(%s,%s,%s,'v1',%s,'{}')""",
                    (uuid4(), outsider, product, "sha256:" + "b" * 64),
                )
        finally:
            conn.rollback()


@pytest.mark.live
def test_catalog_filtered_cursor_and_tenant_isolation():
    url = os.environ.get("ORCA_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("local live catalog database unavailable")
    if "@127.0.0.1:" not in url or "/orca_test" not in url:
        pytest.fail("catalog test requires isolated local orca_test database")
    engine = create_engine(url.replace("postgresql://", "postgresql+psycopg://", 1))
    tenant, outsider = uuid4(), uuid4()
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                for owner in (tenant, outsider):
                    connection.execute(
                        text("""INSERT INTO iam.organizations(id,tenant_id,name,slug)
                      VALUES(:id,:tenant,'test',:slug)"""),
                        {"id": uuid4(), "tenant": owner, "slug": str(owner)},
                    )
                connection.execute(text("SET LOCAL ROLE orca_runtime"))
                with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                    for owner in (tenant, outsider):
                        session.execute(
                            text("SELECT set_config('orca.tenant_id',:tenant,true)"),
                            {"tenant": str(owner)},
                        )
                        service = CatalogService(session, owner)
                        for i in range(4):
                            product = ScientificProduct(
                                id=uuid4(),
                                tenant_id=owner,
                                provider_id=("selected" if i < 3 else "other")
                                if owner == tenant
                                else "outsider-only",
                                source_product_id=str(i),
                                kind="observation" if i < 2 else "forecast",
                            )
                            service.add_product(product)
                            for revision in ("1", "2"):
                                service.add_version(
                                    ProductVersion(
                                        id=uuid4(),
                                        tenant_id=owner,
                                        product_id=product.id,
                                        source_version=revision,
                                        metadata_digest="sha256:" + "a" * 64,
                                        native_variable="sst",
                                        standard_name="sea_surface_temperature",
                                        physical_definition="test-only SST",
                                        native_units="K",
                                        calendar="gregorian",
                                        retrieved_at="2025-02-01T00:00:00Z",
                                        coverage={
                                            "bbox": (68.0, 20.0, 69.0, 21.0),
                                            "start": "2025-01-01T00:00:00Z",
                                            "end": "2025-02-01T00:00:00Z",
                                        }
                                        if i == 0
                                        else None,
                                        license={},
                                    )
                                )
                    session.execute(
                        text("SELECT set_config('orca.tenant_id',:tenant,true)"),
                        {"tenant": str(tenant)},
                    )
                    service = CatalogService(session, tenant)
                    providers = service.providers(limit=1)
                    assert [p.id for p in providers] == ["other"]
                    assert [p.id for p in service.providers(after="other")] == ["selected"]
                    assert service.providers(after="selected") == []
                    assert [p.id for p in service.providers(query="SELECT")] == ["selected"]
                    assert service.providers(query="outsider-only") == []
                    assert service.providers(query="%") == []
                    assert all(p.live_verification == "unknown" for p in service.providers())
                    filters = {
                        "provider_id": "selected",
                        "kind": "observation",
                        "variable": "sea_surface_temperature",
                    }
                    first = service.products(limit=1, **filters)
                    second = service.products(after=first[0].id, limit=1, **filters)
                    assert len(first) == len(second) == 1
                    assert first[0].id != second[0].id
                    versions = service.versions(first[0].id)
                    assert len(versions) == 2
                    assert service.versions(first[0].id, version_id=versions[0].id) == versions[:1]
                    assert service.versions(second[0].id, version_id=versions[0].id) == []
                    assert all(p.tenant_id == tenant for p in first + second)
                    assert service.products(after=second[0].id, **filters) == []
                    assert len(service.products(**filters)) == 2
                    assert service.products(provider_id="' OR true --") == []
                    assert service.products(variable="unknown") == []
                    assert len(service.products(bbox=(68.5, 20.5, 70.0, 22.0), **filters)) == 1
                    assert service.products(bbox=(69.0, 20.0, 70.0, 21.0), **filters) == []
                    assert (
                        len(
                            service.products(
                                start=datetime(2025, 1, 15, tzinfo=timezone.utc), **filters
                            )
                        )
                        == 1
                    )
                    assert (
                        service.products(start=datetime(2025, 2, 1, tzinfo=timezone.utc), **filters)
                        == []
                    )
                    assert (
                        service.products(end=datetime(2025, 1, 1, tzinfo=timezone.utc), **filters)
                        == []
                    )
                    session.rollback()
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
