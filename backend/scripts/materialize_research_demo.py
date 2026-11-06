"""Purpose: materialize the requested demo from captured real ERDDAP responses in Supabase.
Behavior: require actual licensed source bytes, preserve exact content and compute descriptive results.
Refs: accelerated-research-arkiv-demo.json; Phase: accelerated P04/P06/P11 demo.
"""

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
from dotenv import dotenv_values
from sqlalchemy import text

from orca.application.catalog import CatalogService
from orca.application.demo import analyze_snapshot, result_digest
from orca.application.ingestion import preserve_snapshot
from orca.application.snapshots import SnapshotRepository
from orca.config import Settings
from orca.contracts.catalog import ProductVersion, ScientificProduct
from orca.contracts.demo import PublicDemoResult
from orca.contracts.erddap_metadata import parse_metadata
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.storage.composition import snapshot_store

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--subject", type=UUID)
    parser.add_argument("--approve-public-reference", action="store_true")
    args = parser.parse_args()
    raw_metadata = args.metadata.read_bytes()
    content = args.csv.read_bytes()
    document = json.loads(raw_metadata)
    variables = {v.name: v for v in parse_metadata(document)}
    native = variables["analysed_sst"]
    attrs = {(r[1], r[2]): r[4] for r in document["table"]["rows"] if r[0] == "attribute"}
    license_text = attrs.get(("NC_GLOBAL", "license"), "")
    if (
        "may be used and redistributed for free" not in license_text
        or native.standard_name != "sea_surface_foundation_temperature"
    ):
        raise ValueError("Expected explicit MUR license evidence and foundation SST metadata")
    runtime_values = dotenv_values(ROOT / ".env.demo")
    os.environ.update({k: v for k, v in runtime_values.items() if v is not None})
    settings = Settings()  # type: ignore[call-arg]
    private = dotenv_values(ROOT / ".env")
    admin = private.get("ORCA_MIGRATION_DATABASE_URL") or private["ORCA_TEST_DATABASE_URL"]
    assert admin
    tenant, organization, product = uuid4(), uuid4(), uuid4()
    with psycopg.connect(admin, connect_timeout=10) as connection:
        if (
            args.subject
            and connection.execute(
                "SELECT 1 FROM auth.users WHERE id=%s", (args.subject,)
            ).fetchone()
            is None
        ):
            raise ValueError("Subject must be an existing real Supabase Auth user")
        connection.execute(
            "INSERT INTO iam.organizations(id,tenant_id,name,slug) VALUES(%s,%s,'ORCA research demo',%s)",
            (organization, tenant, f"demo-{tenant}"),
        )
        if args.subject:
            connection.execute(
                "INSERT INTO iam.memberships(id,tenant_id,subject,role) VALUES(%s,%s,%s,'researcher')",
                (uuid4(), tenant, str(args.subject)),
            )
    store = snapshot_store(settings)
    client = cast(Any, store.client)
    if settings.object_store_bucket not in {
        b["Name"] for b in client.list_buckets().get("Buckets", [])
    }:
        client.create_bucket(Bucket=settings.object_store_bucket)
    assert settings.database_url
    engine = create_runtime_engine(settings.database_url)
    captured_at = datetime.fromtimestamp(args.csv.stat().st_mtime, timezone.utc)
    version = ProductVersion.model_validate(
        {
            "id": uuid4(),
            "tenant_id": tenant,
            "product_id": product,
            "source_version": "MUR-JPL-L4-GLOB-v4.1",
            "metadata_digest": "sha256:" + hashlib.sha256(raw_metadata).hexdigest(),
            "native_variable": native.name,
            "standard_name": native.standard_name,
            "physical_definition": "MUR foundation SST; descriptive access-sample demo",
            "native_units": native.native_units,
            "dimensions": native.dimensions,
            "calendar": "gregorian",
            "retrieved_at": datetime.fromtimestamp(args.metadata.stat().st_mtime, timezone.utc),
            "license": {
                "license_ref": "JPL-PODAAC-data-policy",
                "evidence_digest": "sha256:" + hashlib.sha256(license_text.encode()).hexdigest(),
                "can_preserve": True,
                "can_redistribute": True,
            },
        }
    )
    try:
        with TenantUnitOfWork(engine, tenant) as session:
            catalog = CatalogService(session, tenant)
            catalog.add_product(
                ScientificProduct(
                    id=product,
                    tenant_id=tenant,
                    provider_id="NASA-JPL-PO.DAAC",
                    source_product_id="jplMURSST41",
                    kind="derived",
                )
            )
            catalog.add_version(version)
        manifest = preserve_snapshot(
            store,
            tenant,
            version,
            content,
            origin_uri="https://coastwatch.pfeg.noaa.gov/erddap/griddap/jplMURSST41.csv",
            retrieved_at=captured_at,
        )
        result = analyze_snapshot(manifest, content)
        with TenantUnitOfWork(engine, tenant) as session:
            SnapshotRepository(session, tenant).add(manifest)
            session.execute(
                text("""INSERT INTO research.demo_results(id,tenant_id,snapshot_id,result_digest,manifest)
              VALUES(:id,:tenant,:snapshot,:digest,CAST(:manifest AS jsonb))"""),
                {
                    "id": result.id,
                    "tenant": tenant,
                    "snapshot": manifest.id,
                    "digest": result_digest(result),
                    "manifest": result.model_dump_json(),
                },
            )
        folder = ROOT / ".cache/demo"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "research-result.json").write_text(
            result.model_dump_json(indent=2), encoding="utf-8"
        )
        if args.approve_public_reference:
            public = PublicDemoResult(result_id=result.id, result_digest=result_digest(result))
            (folder / "public-result.json").write_text(
                public.model_dump_json(indent=2), encoding="utf-8"
            )
        summary = {
            "organization_id": str(organization),
            "tenant_id": str(tenant),
            "snapshot_id": str(manifest.id),
            "result_id": str(result.id),
            "result_digest": result_digest(result),
            "row_count": result.row_count,
            "finite_count": result.finite_count,
            "scientific_status": result.scientific_status,
            "auth_membership_created": bool(args.subject),
        }
        (folder / "cloud-demo.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2))
    finally:
        engine.dispose()
        client.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("Demo materialization failed:", type(error).__name__)
        raise SystemExit(1) from None
