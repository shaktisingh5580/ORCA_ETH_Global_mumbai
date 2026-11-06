"""Purpose: preserve the qualified real native SST input set in private cloud storage.
Behavior: revalidate captures, pin exact source identity and persist resumable immutable manifests.
Refs: ADR-R02; main plan P03.3/P03.4; API 3.4; Phase: P03; trusted operator tooling.
"""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import text

from orca.application.catalog import CatalogService
from orca.application.ingestion import preserve_snapshot
from orca.application.snapshots import SnapshotRepository
from orca.application.workspace_access import authorize_project
from orca.config import Settings
from orca.contracts.catalog import Coverage, LicenseProfile, ProductVersion
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.domain.scientific_inputs import (
    NativeGrid,
    parse_native_month,
    qualify_availability,
    qualify_metadata,
    qualify_seasonal_inputs,
)
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.tenant_context import assert_runtime_role
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.storage.composition import snapshot_store

ROOT = Path(__file__).resolve().parents[2]
YEARS = tuple(range(2003, 2023))


def digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--organization-id", required=True, type=UUID)
    parser.add_argument("--project-id", required=True, type=UUID)
    parser.add_argument("--product-id", required=True, type=UUID)
    parser.add_argument("--subject", required=True)
    parser.add_argument(
        "--cache-dir", type=Path, default=ROOT / ".cache/science-inputs/mur-native-batch"
    )
    args = parser.parse_args()
    folder = args.cache_dir.resolve()
    capture = json.loads((folder / "capture.json").read_text(encoding="utf-8"))
    if (
        capture.get("fixture") is not False
        or capture.get("source_host") != "coastwatch.pfeg.noaa.gov"
        or capture.get("dataset") != "jplMURSST41"
    ):
        raise ValueError("approved real source capture required")
    metadata_raw = (folder / "metadata.json").read_bytes()
    if digest(metadata_raw) != capture["metadata"]["sha256"]:
        raise ValueError("metadata capture digest mismatch")
    metadata = json.loads(metadata_raw)
    proof = qualify_metadata(metadata)
    grid = NativeGrid(68.45, 20.45, 68.55, 20.55, proof.longitude_step, proof.latitude_step)
    parsed = {}
    for year in (2025, *YEARS):
        raw = (folder / f"{year}-01.csv").read_bytes()
        record = capture["inputs"][str(year)]
        if digest(raw) != record["sha256"] or len(raw) != record["bytes"]:
            raise ValueError("source capture content changed")
        parsed[year] = parse_native_month(raw, year, 1, grid, proof)
    qualification = qualify_seasonal_inputs(parsed[2025], tuple(parsed[y] for y in YEARS), YEARS)
    as_of = datetime.now(timezone.utc)
    qualify_availability(
        tuple(
            datetime.fromisoformat(record["retrieved_at"]) for record in capture["inputs"].values()
        )
        + (datetime.fromisoformat(capture["metadata"]["retrieved_at"]),),
        as_of,
    )
    attrs = {(row[1], row[2]): row[4] for row in metadata["table"]["rows"] if row[0] == "attribute"}
    settings = Settings(_env_file=args.env_file)  # type: ignore[call-arg]
    if not settings.database_url:
        raise ValueError("configured private runtime database required")
    db = create_runtime_engine(settings.database_url)
    store = snapshot_store(settings)
    principal = Principal(
        args.subject, "trusted-local-operator", frozenset().union(*ROLE_SCOPES.values())
    )
    try:
        with db.begin() as connection:
            assert_runtime_role(connection)
            tenant: UUID | None = connection.execute(
                text("SELECT iam.resolve_tenant(:s,:org)"),
                {"s": args.subject, "org": args.organization_id},
            ).scalar_one_or_none()
        if tenant is None:
            raise PermissionError("active organization membership required")
        with TenantUnitOfWork(db, tenant) as session:
            authorize_project(session, tenant, args.project_id, principal, "project:write")
            principal.require(
                "dataset:read",
                set(
                    session.execute(
                        text(
                            "SELECT role FROM iam.memberships WHERE tenant_id=:t AND subject=:s AND status='active'"
                        ),
                        {"t": tenant, "s": args.subject},
                    ).scalars()
                ),
            )
            product = CatalogService(session, tenant).product(args.product_id)
            if (
                product is None
                or product.source_product_id != "jplMURSST41"
                or product.provider_id != "NASA-JPL-PO.DAAC"
            ):
                raise ValueError("registered matching source product required")
            # Preserve the exact upstream id (including 04.1), not a fabricated vendor version.
            source_version = attrs["NC_GLOBAL", "id"]
            existing = session.execute(
                text(
                    "SELECT manifest FROM research.catalog_versions WHERE tenant_id=:t AND product_id=:p AND source_version=:v AND metadata_digest=:d"
                ),
                {"t": tenant, "p": args.product_id, "v": source_version, "d": digest(metadata_raw)},
            ).scalar_one_or_none()
            version = (
                ProductVersion.model_validate(existing)
                if existing
                else ProductVersion(
                    id=uuid4(),
                    tenant_id=tenant,
                    product_id=args.product_id,
                    source_version=source_version,
                    metadata_digest=digest(metadata_raw),
                    native_variable="analysed_sst",
                    standard_name="sea_surface_foundation_temperature",
                    physical_definition="GHRSST MUR L4 foundation SST analysis; matching source semantics without an invented fixed depth.",
                    native_units=cast(Literal["degree_C", "degree_Celsius"], proof.units),
                    dimensions=("time", "latitude", "longitude"),
                    calendar="gregorian",
                    retrieved_at=capture["metadata"]["retrieved_at"],
                    available_at=capture["metadata"]["retrieved_at"],
                    license=LicenseProfile.model_validate(
                        {
                            "license_ref": "JPL-PODAAC-data-policy",
                            "evidence_digest": digest(attrs["NC_GLOBAL", "license"].encode()),
                            "can_preserve": True,
                            "can_redistribute": True,
                        }
                    ),
                    coverage=Coverage.model_validate(
                        {
                            "bbox": [
                                float(attrs["NC_GLOBAL", "geospatial_lon_min"]),
                                float(attrs["NC_GLOBAL", "geospatial_lat_min"]),
                                float(attrs["NC_GLOBAL", "geospatial_lon_max"]),
                                float(attrs["NC_GLOBAL", "geospatial_lat_max"]),
                            ],
                            "start": attrs["NC_GLOBAL", "time_coverage_start"],
                            "end": attrs["NC_GLOBAL", "time_coverage_end"],
                        }
                    ),
                )
            )
            if existing is None:
                CatalogService(session, tenant).add_version(version)
        if store.preserve(tenant, metadata_raw, permitted=version.license.can_preserve) != digest(
            metadata_raw
        ):
            raise ValueError("metadata preservation digest differs")
        references = []
        for year in (2025, *YEARS):
            record = capture["inputs"][str(year)]
            raw = (folder / f"{year}-01.csv").read_bytes()
            identifier = uuid5(
                NAMESPACE_URL,
                f"orca-native-input:{tenant}:{version.id}:{record['sha256']}:{record['retrieved_at']}:{record['query']}",
            )
            with TenantUnitOfWork(db, tenant) as session:
                authorize_project(session, tenant, args.project_id, principal, "project:write")
                repository = SnapshotRepository(session, tenant)
                manifest = repository.get(identifier)
                if manifest is None:
                    manifest = preserve_snapshot(
                        store,
                        tenant,
                        version,
                        raw,
                        origin_uri="https://coastwatch.pfeg.noaa.gov/erddap/griddap/jplMURSST41.csv",
                        retrieved_at=datetime.fromisoformat(record["retrieved_at"]),
                    )
                    manifest = manifest.model_copy(
                        update={
                            "id": identifier,
                            "quality_policy_ref": "ADR-R02-native-open-sea-v1",
                        }
                    )
                    repository.add(manifest)
                if (
                    manifest.raw_content_digest != record["sha256"]
                    or store.read(tenant, manifest.raw_content_digest) != raw
                ):
                    raise ValueError("stored scientific input read-back differs")
                repository.grant_project(identifier, args.project_id)
            references.append(
                {
                    "year": year,
                    "role": "current" if year == 2025 else "baseline",
                    "snapshot_id": str(identifier),
                    **record,
                }
            )
            print(json.dumps({"year": year, "status": "PRESERVED_AND_READ_BACK"}), flush=True)
        bundle = {
            "schema_version": "orca.scientific-input-bundle/1.0",
            "tenant_id": str(tenant),
            "project_id": str(args.project_id),
            "version": version.model_dump(mode="json"),
            "policy": "ADR-R02",
            "policy_digest": digest(
                (ROOT / "docs/adr/ADR-R02-native-mur-inputs.json").read_bytes()
            ),
            "qualifier_digest": digest(
                (ROOT / "backend/src/orca/domain/scientific_inputs.py").read_bytes()
            ),
            "metadata_digest": digest(metadata_raw),
            "qualification": qualification,
            "bbox": [68.45, 20.45, 68.55, 20.55],
            "analysis_as_of": as_of.isoformat(),
            "availability_policy": "first observed on retrieval; no 2025 historical availability claim",
            "acknowledgment": attrs.get(("NC_GLOBAL", "acknowledgement")),
            "members": references,
        }
        bundle_bytes = json.dumps(
            bundle, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        bundle_digest = store.preserve(tenant, bundle_bytes, permitted=version.license.can_preserve)
        if store.read(tenant, bundle_digest) != bundle_bytes:
            raise ValueError("input bundle read-back differs")
        evidence = {
            "phase": "P03.4",
            "status": "REAL_INPUTS_PRESERVED_COMPATIBILITY_VERIFIED",
            "fixture": False,
            "source_host": "coastwatch.pfeg.noaa.gov",
            "source_dataset": "jplMURSST41",
            "project_id": str(args.project_id),
            "version_id": str(version.id),
            "bundle_digest": bundle_digest,
            "metadata_digest": digest(metadata_raw),
            "qualification": qualification,
            "analysis_as_of": as_of.isoformat(),
            "snapshots": [
                {
                    "year": r["year"],
                    "id": r["snapshot_id"],
                    "sha256": r["sha256"],
                    "bytes": r["bytes"],
                }
                for r in references
            ],
            "method_science_validated": False,
            "gate_G03_pass": False,
        }
        output = ROOT / "docs/gates/P03/native-inputs-cloud.json"
        output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(
            json.dumps(
                {
                    "status": evidence["status"],
                    "bundle_digest": bundle_digest,
                    "snapshots": len(references),
                }
            ),
            flush=True,
        )
    finally:
        db.dispose()


if __name__ == "__main__":
    main()
