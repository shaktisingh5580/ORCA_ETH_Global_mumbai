"""Purpose: calculate the hackathon's bounded seasonal SST comparison from actual source cells.
Behavior: clip inferred native cell bounds, use spherical area weights and equal daily/year weights.
Refs: hackathon plan H02; ADR-R02; docs/07_scientific_engine.md; Phase: H02.
"""

import math
from collections import defaultdict
from typing import Any

from orca.domain.scientific_inputs import NativeMonth, qualify_seasonal_inputs

METHOD = "orca.native-seasonal-sst/hackathon-v1"


def seasonal_anomaly(current: NativeMonth, baseline: tuple[NativeMonth, ...]) -> dict[str, Any]:
    qualification = qualify_seasonal_inputs(
        current, baseline, tuple(sorted(item.year for item in baseline))
    )
    grid = current.grid
    height, width = grid.shape()
    weights = {}
    for y in range(height):
        center = grid.south + y * grid.latitude_step
        south = max(grid.south, center - grid.latitude_step / 2)
        north = min(grid.north, center + grid.latitude_step / 2)
        for x in range(width):
            longitude = grid.west + x * grid.longitude_step
            west = max(grid.west, longitude - grid.longitude_step / 2)
            east = min(grid.east, longitude + grid.longitude_step / 2)
            weights[y, x] = math.radians(east - west) * (
                math.sin(math.radians(north)) - math.sin(math.radians(south))
            )
    total_weight = math.fsum(weights.values())
    if total_weight <= 0:
        raise ValueError("positive requested ocean area required")
    reference = defaultdict(list)
    for table in baseline:
        for cell in table.cells:
            reference[cell.day, cell.latitude_index, cell.longitude_index].append(cell.sst)
    baseline_means = {key: math.fsum(values) / len(values) for key, values in reference.items()}
    daily = defaultdict(list)
    for cell in current.cells:
        key = (cell.day, cell.latitude_index, cell.longitude_index)
        daily[cell.day].append(
            (weights[cell.latitude_index, cell.longitude_index], cell.sst, baseline_means[key])
        )
    series: list[dict[str, Any]] = []
    for day, values in sorted(daily.items()):
        observed = math.fsum(weight * value for weight, value, _ in values) / total_weight
        climatology = math.fsum(weight * value for weight, _, value in values) / total_weight
        series.append(
            {
                "date": f"{current.year}-{current.month:02d}-{day:02d}",
                "current_mean": observed,
                "baseline_mean": climatology,
                "anomaly": observed - climatology,
            }
        )
    current_mean = math.fsum(row["current_mean"] for row in series) / len(series)
    baseline_mean = math.fsum(row["baseline_mean"] for row in series) / len(series)
    return {
        "method": METHOD,
        "source_mode": "real",
        "units": "degree_Celsius",
        "current_mean": current_mean,
        "baseline_mean": baseline_mean,
        "anomaly": current_mean - baseline_mean,
        "daily": series,
        "bbox": [grid.west, grid.south, grid.east, grid.north],
        "area_km2": 6371008.8**2 * total_weight / 1_000_000,
        "spatial_coverage": 1.0,
        "temporal_coverage": 1.0,
        "current_cells": qualification["current_cells"],
        "baseline_cells": qualification["baseline_cells"],
        "baseline_years": qualification["baseline_years"],
        "scientific_status": "prototype_not_independently_validated",
        "uncertainty": "unknown; source errors share information and aggregate covariance is not established",
        "limitations": [
            f"{len(baseline)}-year product-specific seasonal reference, not a 30-year climate normal.",
            "Uniform native-cell bounds and spherical Earth area approximation.",
            "Research prototype; independent domain/method qualification remains deferred.",
        ],
    }
