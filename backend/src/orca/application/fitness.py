"""Purpose: evaluate explicit stored metadata without inventing scientific readiness.
Behavior: reject spatial/time/rights/semantic conflicts; unknown evidence never becomes a pass.
Refs: API 3.4; main plan P03.5; research-only guide 5; Phase: P03.
"""

from shapely.geometry import shape

from orca.contracts.catalog import ProductVersion
from orca.contracts.fitness import FitnessCheck, FitnessReport, FitnessRequest


def check(name: str, condition: bool | None, reason: str) -> FitnessCheck:
    return FitnessCheck(
        name=name,
        status="unknown" if condition is None else "pass" if condition else "fail",
        reason=reason,
    )


def fitness(version: ProductVersion, request: FitnessRequest) -> FitnessReport:
    coverage = version.coverage
    bounds = shape(request.geo_scope.geometry.model_dump()).bounds
    spatial = (
        None
        if coverage is None
        else (
            coverage.bbox[0] <= bounds[0]
            and coverage.bbox[1] <= bounds[1]
            and coverage.bbox[2] >= bounds[2]
            and coverage.bbox[3] >= bounds[3]
        )
    )
    temporal = (
        None
        if coverage is None
        else (
            coverage.start <= request.time_window.start and coverage.end >= request.time_window.end
        )
    )
    checks = [
        check(
            "preservation_rights",
            version.license.can_preserve,
            "Stored license evidence must permit preservation",
        ),
        check(
            "spatial_extent",
            spatial,
            "Metadata bounding box containment; not valid-observation coverage",
        ),
        check(
            "temporal_extent",
            temporal,
            "Metadata time extent containment; not gap-free observations",
        ),
        check(
            "calendar",
            None
            if request.time_window.calendar is None
            else version.calendar == request.time_window.calendar,
            "Explicit method calendar is required",
        ),
        check(
            "depth",
            None
            if request.depth is None or version.depth_m is None
            else request.depth == version.depth_m,
            "Explicit matching measurement depth is required",
        ),
        check(
            "method_and_qc",
            None,
            "Pinned method requirements and source QC have not been validated",
        ),
    ]
    if request.as_of is not None:
        checks.append(
            check(
                "as_of",
                None if version.available_at is None else version.available_at <= request.as_of,
                "Source availability must be established at the requested cutoff",
            )
        )
    return FitnessReport(
        status="incompatible" if any(c.status == "fail" for c in checks) else "unknown",
        method_id=request.method_id,
        version_refs=[version.id],
        checks=checks,
    )


def compare_versions(product: ProductVersion, baseline: ProductVersion) -> list[FitnessCheck]:
    return [
        check(
            "measurement_definition",
            product.standard_name == baseline.standard_name
            and product.physical_definition == baseline.physical_definition,
            "Measurement name and physical definition must match explicitly",
        ),
        check(
            "units",
            product.native_units == baseline.native_units,
            "No implicit unit conversion without a pinned method",
        ),
        check("calendar", product.calendar == baseline.calendar, "Calendars must agree"),
        check(
            "depth",
            None
            if product.depth_m is None or baseline.depth_m is None
            else product.depth_m == baseline.depth_m,
            "Unknown depth is not compatible depth",
        ),
        check(
            "baseline_season_and_qc",
            None,
            "Baseline season, aggregation, period and QC require scientific validation",
        ),
    ]
