"""Purpose: recompute the registered public-source study inside separately keyed agent processes.
Behavior: verify exact captured bytes and use a separate averaging order for numerical review.
Refs: H04 agent decision; ADR-R02; native seasonal recipe; Phase: H04.
"""

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from orca.domain.prototype_analysis import seasonal_anomaly
from orca.domain.scientific_inputs import (
    NativeGrid,
    NativeMonth,
    parse_native_month,
    qualify_metadata,
    qualify_seasonal_inputs,
)
from orca.infrastructure.federation.signing import digest


def reproduce(folder: Path, recipe: dict[str, Any], *, review: bool) -> dict[str, Any]:
    if recipe["method"] != "orca.native-seasonal-sst/hackathon-v1" or recipe["bbox"] != [
        68.45,
        20.45,
        68.55,
        20.55,
    ]:
        raise ValueError("unsupported delegated method or region")
    metadata = (folder / "metadata.json").read_bytes()
    if "sha256:" + hashlib.sha256(metadata).hexdigest() != recipe["metadata_digest"]:
        raise ValueError("provider metadata differs from delegated input")
    proof = qualify_metadata(json.loads(metadata))
    west, south, east, north = recipe["bbox"]
    grid = NativeGrid(west, south, east, north, proof.longitude_step, proof.latitude_step)
    members = recipe["input_snapshots"]
    if len(members) != 21 or {member["year"] for member in members} != {2025, *range(2003, 2023)}:
        raise ValueError("complete registered seasonal inputs required")
    parsed = {}
    for member in members:
        year = member["year"]
        raw = (folder / f"{year}-01.csv").read_bytes()
        if "sha256:" + hashlib.sha256(raw).hexdigest() != member["sha256"]:
            raise ValueError("provider raw source differs from delegated snapshot")
        parsed[year] = parse_native_month(raw, year, 1, grid, proof)
    current, baseline = parsed[2025], tuple(parsed[year] for year in range(2003, 2023))
    qualify_seasonal_inputs(current, baseline, tuple(range(2003, 2023)))
    if review:
        actual = alternate_reduction(current, baseline)
        implementation = "separate-year-first-regional-reduction/hackathon-v1"
    else:
        full = seasonal_anomaly(current, baseline)
        actual = {key: full[key] for key in ("current_mean", "baseline_mean", "anomaly", "daily")}
        implementation = "shared-native-seasonal-method/reexecution"
    expected = recipe["expected"]
    differences = [
        abs(actual[key] - expected[key]) for key in ("current_mean", "baseline_mean", "anomaly")
    ]
    if len(actual["daily"]) != len(expected["daily"]) or [
        row["date"] for row in actual["daily"]
    ] != [row["date"] for row in expected["daily"]]:
        raise ValueError("reference daily support differs")
    for observed, reference in zip(actual["daily"], expected["daily"], strict=True):
        differences.extend(
            abs(observed[key] - reference[key])
            for key in ("current_mean", "baseline_mean", "anomaly")
        )
    maximum = max(differences)
    return {
        **actual,
        "verdict": "reproduced" if maximum <= 1e-9 else "challenged",
        "maximum_absolute_difference": maximum,
        "absolute_tolerance_celsius": 1e-9,
        "implementation": implementation,
        "raw_source_digests_verified": len(members),
        "input_manifest_digest": digest(
            {
                "metadata_digest": recipe["metadata_digest"],
                "inputs": sorted(
                    [{"year": member["year"], "sha256": member["sha256"]} for member in members],
                    key=lambda item: item["year"],
                ),
            }
        ),
        "source_mode": "real",
        "operator_mode": "team_controlled",
        "scientific_status": "prototype_not_independently_validated",
        "limitations": [
            "Separate process and key do not establish independent institutional operation.",
            "Review checks numerical reproducibility on shared preserved inputs, not source independence or physical truth.",
            "Twenty-year seasonal reference; aggregate uncertainty remains unknown.",
        ],
    }


def alternate_reduction(current: NativeMonth, baseline: tuple[NativeMonth, ...]) -> dict[str, Any]:
    # Unlike the producer's cellwise baseline-first implementation, reduce each year's
    # regional daily observations first, then average equally across reference years.
    grid = current.grid
    weights = {}
    for cell in current.cells:
        y, x = cell.latitude_index, cell.longitude_index
        latitude = grid.south + y * grid.latitude_step
        longitude = grid.west + x * grid.longitude_step
        south, north = (
            max(grid.south, latitude - grid.latitude_step / 2),
            min(grid.north, latitude + grid.latitude_step / 2),
        )
        west, east = (
            max(grid.west, longitude - grid.longitude_step / 2),
            min(grid.east, longitude + grid.longitude_step / 2),
        )
        weights[y, x] = (east - west) * (
            math.sin(math.radians(north)) - math.sin(math.radians(south))
        )
    total = math.fsum(weights.values())

    def regional(table: NativeMonth, day: int) -> float:
        return (
            math.fsum(
                weights[cell.latitude_index, cell.longitude_index] * cell.sst
                for cell in table.cells
                if cell.day == day
            )
            / total
        )

    daily: list[dict[str, Any]] = []
    for day in range(1, 32):
        observed = regional(current, day)
        reference = math.fsum(regional(table, day) for table in baseline) / len(baseline)
        daily.append(
            {
                "date": f"2025-01-{day:02d}",
                "current_mean": observed,
                "baseline_mean": reference,
                "anomaly": observed - reference,
            }
        )
    observed_mean = math.fsum(row["current_mean"] for row in daily) / 31
    baseline_mean = math.fsum(row["baseline_mean"] for row in daily) / 31
    return {
        "current_mean": observed_mean,
        "baseline_mean": baseline_mean,
        "anomaly": observed_mean - baseline_mean,
        "daily": daily,
    }
