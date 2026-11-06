"""Purpose: prove metadata conflicts cannot become scientific compatibility claims.
Behavior: use synthetic stored versions only in tests and inspect fail/unknown outcomes.
Refs: API 3.4; main plan P03.5; Phase: P03.
"""

from uuid import uuid4

from orca.application.fitness import compare_versions, fitness
from orca.contracts.catalog import ProductVersion
from orca.contracts.fitness import FitnessRequest


def test_fitness_unknown_and_explicit_coverage_conflict():
    version = ProductVersion(
        id=uuid4(),
        tenant_id=uuid4(),
        product_id=uuid4(),
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
    body = FitnessRequest(
        organization_id=uuid4(),
        method_id="sst",
        version_ref=version.id,
        geo_scope={"type": "Point", "geometry": {"type": "Point", "coordinates": [68.5, 20.5]}},
        time_window={"start": "2025-01-01T00:00:00Z", "end": "2025-01-02T00:00:00Z"},
    )
    report = fitness(version, body)
    assert report.status == "unknown" and report.scientific_validation == "not_validated"
    data = version.model_dump(mode="json")
    data["coverage"] = {
        "bbox": [70, 20, 71, 21],
        "start": "2025-01-01T00:00:00Z",
        "end": "2025-02-01T00:00:00Z",
    }
    report = fitness(ProductVersion.model_validate(data), body)
    assert report.status == "incompatible"
    assert next(c for c in report.checks if c.name == "spatial_extent").status == "fail"
    baseline = version.model_copy(update={"standard_name": "sea_surface_foundation_temperature"})
    checks = {c.name: c.status for c in compare_versions(version, baseline)}
    assert checks["measurement_definition"] == "fail"
    assert checks["depth"] == "unknown" and checks["baseline_season_and_qc"] == "unknown"
