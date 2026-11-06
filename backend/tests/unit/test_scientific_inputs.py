"""Purpose: reject fabricated compatibility and incomplete native-grid seasonal inputs.
Behavior: use explicitly synthetic test-only CSV/metadata to exercise independent failure cases.
Refs: main plan P03.3/P03.4; ADR-R02; Phase: P03; fixtures confined to tests.
"""

import csv
import io
from dataclasses import replace

import pytest

from orca.domain.scientific_inputs import (
    MetadataProof,
    NativeGrid,
    parse_native_month,
    qualify_metadata,
    qualify_seasonal_inputs,
)


def source_metadata():
    rows = []
    for name in ("analysed_sst", "analysis_error", "mask"):
        rows.append(["variable", name, "", "double", "time, latitude, longitude"])
    attrs = {
        ("analysed_sst", "standard_name"): "sea_surface_foundation_temperature",
        ("analysed_sst", "units"): "degree_C",
        ("analysis_error", "units"): "degree_C",
        ("analysis_error", "long_name"): "Estimated Error Standard Deviation of analysed_sst",
        ("mask", "flag_masks"): "1, 2, 4, 8, 16",
        (
            "mask",
            "flag_meanings",
        ): "open_sea land open_lake open_sea_with_ice_in_the_grid open_lake_with_ice_in_the_grid",
        ("NC_GLOBAL", "Conventions"): "CF-1.6",
        ("NC_GLOBAL", "license"): "Test-only license: data may be used and redistributed for free",
        ("NC_GLOBAL", "geospatial_lat_resolution"): "0.01",
        ("NC_GLOBAL", "geospatial_lon_resolution"): "0.01",
        ("analysed_sst", "valid_min"): "-2",
        ("analysed_sst", "valid_max"): "40",
        ("analysis_error", "valid_max"): "10",
        ("analysis_error", "valid_min"): "0",
        ("analysed_sst", "_FillValue"): "-999",
        ("analysis_error", "_FillValue"): "-999",
        ("latitude", "units"): "degrees_north",
        ("longitude", "units"): "degrees_east",
    }
    rows.extend(
        ["attribute", name, attribute, "String", value]
        for (name, attribute), value in attrs.items()
    )
    return {
        "table": {
            "columnNames": ["Row Type", "Variable Name", "Attribute Name", "Data Type", "Value"],
            "rows": rows,
        }
    }


GRID = NativeGrid(68.45, 20.45, 68.46, 20.46, 0.01, 0.01)
PROOF = MetadataProof("degree_C", 0.01, 0.01, -2, 40, 10)


def csv_month(year, *, mask=1, omit_last=False, duplicate=False, units="degree_C"):
    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(["time", "latitude", "longitude", "analysed_sst", "analysis_error", "mask"])
    writer.writerow(["UTC", "degrees_north", "degrees_east", units, units, ""])
    rows = [
        [f"{year}-01-{day:02d}T09:00:00Z", lat, lon, 26, 0.5, mask]
        for day in range(1, 32)
        for lat in (20.45, 20.46)
        for lon in (68.45, 68.46)
    ]
    writer.writerows(rows[:-1] if omit_last else rows)
    if duplicate:
        writer.writerow(rows[0])
    return stream.getvalue().encode()


def test_metadata_requires_rights_and_explicit_foundation_mask_calendar_semantics():
    assert qualify_metadata(source_metadata()) == PROOF
    for name, attribute, value in [
        ("analysed_sst", "standard_name", "sea_water_potential_temperature"),
        ("analysis_error", "units", "K"),
        ("mask", "flag_masks", "2, 1, 4, 8, 16"),
        ("time", "calendar", "360_day"),
    ]:
        metadata = source_metadata()
        metadata["table"]["rows"] = [
            r
            for r in metadata["table"]["rows"]
            if not (r[0] == "attribute" and r[1] == name and r[2] == attribute)
        ]
        metadata["table"]["rows"].append(["attribute", name, attribute, "String", value])
        with pytest.raises(ValueError):
            qualify_metadata(metadata)
    metadata = source_metadata()
    metadata["table"]["rows"] = [r for r in metadata["table"]["rows"] if r[2] != "license"]
    with pytest.raises(PermissionError):
        qualify_metadata(metadata)


def test_native_input_rejects_missing_duplicate_subsampled_or_wrong_unit_cells():
    for body in (
        csv_month(2025, omit_last=True),
        csv_month(2025, duplicate=True),
        csv_month(2025, units="K"),
    ):
        with pytest.raises(ValueError):
            parse_native_month(body, 2025, 1, GRID, PROOF)
    with pytest.raises(ValueError):
        parse_native_month(csv_month(2025), 2025, 1, replace(GRID, latitude_step=0.1), PROOF)
    with pytest.raises(ValueError):
        parse_native_month(csv_month(2024), 2025, 1, GRID, PROOF)


def test_baseline_season_depth_calendar_compatibility():
    current = parse_native_month(csv_month(2025), 2025, 1, GRID, PROOF)
    baseline = tuple(parse_native_month(csv_month(y), y, 1, GRID, PROOF) for y in (2003, 2004))
    accepted = qualify_seasonal_inputs(current, baseline, (2003, 2004))
    assert accepted["baseline_cells"] == 248 and accepted["method_science_validated"] is False
    for changed, years in [
        (baseline[:1], (2003, 2004)),
        ((baseline[0], baseline[0]), (2003, 2004)),
        ((replace(baseline[0], month=2), baseline[1]), (2003, 2004)),
        ((replace(baseline[0], nominal_time_utc="10:00:00.000000"), baseline[1]), (2003, 2004)),
        ((replace(baseline[0], grid=replace(GRID, west=68.44)), baseline[1]), (2003, 2004)),
        ((replace(baseline[0], year=2026), baseline[1]), (2026, 2004)),
    ]:
        with pytest.raises(ValueError):
            qualify_seasonal_inputs(current, changed, years)


@pytest.mark.parametrize("mask", [0, 2, 4, 8, 16, -128])
def test_qc_or_land_ice_masks_cannot_qualify_as_open_ocean(mask):
    current = parse_native_month(csv_month(2025), 2025, 1, GRID, PROOF)
    invalid = parse_native_month(csv_month(2003, mask=mask), 2003, 1, GRID, PROOF)
    assert not any(cell.valid for cell in invalid.cells)
    with pytest.raises(ValueError):
        qualify_seasonal_inputs(current, (invalid,), (2003,))
