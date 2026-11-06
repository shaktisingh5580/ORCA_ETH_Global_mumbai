"""Purpose: qualify native-grid seasonal SST inputs without calculating an anomaly.
Behavior: validate primary metadata, exact daily grids, QC/masks, seasons and baseline years.
Refs: main plan P03.3/P03.4; ADR-R02; docs/07_scientific_engine.md; Phase: P03.
"""

import calendar
import csv
import io
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class NativeGrid:
    west: float
    south: float
    east: float
    north: float
    longitude_step: float
    latitude_step: float

    def shape(self) -> tuple[int, int]:
        if not (-180 <= self.west < self.east <= 180 and -90 <= self.south < self.north <= 90):
            raise ValueError("ordered bounded native-grid region required")
        spans = (
            (self.north - self.south, self.latitude_step),
            (self.east - self.west, self.longitude_step),
        )
        result = []
        for span, step in spans:
            if not math.isfinite(step) or step <= 0:
                raise ValueError("finite native grid spacing required")
            intervals = span / step
            if not math.isclose(intervals, round(intervals), abs_tol=1e-5):
                raise ValueError("first input benchmark requires grid-aligned bounds")
            result.append(round(intervals) + 1)
        if result[0] * result[1] > 10000:
            raise ValueError("scientific input probe grid exceeds bound")
        return result[0], result[1]


@dataclass(frozen=True)
class MetadataProof:
    units: str
    latitude_step: float
    longitude_step: float
    sst_min: float
    sst_max: float
    error_max: float
    measurement: str = "sea_surface_foundation_temperature"
    calendar: str = "CF-standard-post-1582-Gregorian"


@dataclass(frozen=True)
class SSTCell:
    day: int
    latitude_index: int
    longitude_index: int
    sst: float
    analysis_error: float
    mask: int
    valid: bool


@dataclass(frozen=True)
class NativeMonth:
    year: int
    month: int
    grid: NativeGrid
    cells: tuple[SSTCell, ...]
    nominal_time_utc: str


def qualify_metadata(document: Any) -> MetadataProof:
    table = document.get("table") if isinstance(document, dict) else None
    if not isinstance(table, dict) or table.get("columnNames") != [
        "Row Type",
        "Variable Name",
        "Attribute Name",
        "Data Type",
        "Value",
    ]:
        raise ValueError("pinned ERDDAP metadata table required")
    attributes: dict[tuple[str, str], str] = {}
    variables = {}
    for row in table.get("rows", []):
        if not isinstance(row, list) or len(row) != 5 or not all(isinstance(x, str) for x in row):
            raise ValueError("malformed source metadata")
        kind, name, attribute, _, value = row
        if kind == "attribute":
            if (name, attribute) in attributes:
                raise ValueError("duplicate source metadata attribute")
            attributes[name, attribute] = value
        elif kind == "variable":
            if name in variables:
                raise ValueError("duplicate source variable")
            variables[name] = tuple(x.strip() for x in value.split(","))
    for name in ("analysed_sst", "analysis_error", "mask"):
        if variables.get(name) != ("time", "latitude", "longitude"):
            raise ValueError("source variables must share the declared daily spatial grid")
    if attributes.get(("analysed_sst", "standard_name")) != "sea_surface_foundation_temperature":
        raise ValueError("foundation SST semantics required; depth is not assumed zero")
    units = attributes.get(("analysed_sst", "units"), "")
    if (
        units not in {"degree_C", "degree_Celsius"}
        or attributes.get(("analysis_error", "units")) != units
    ):
        raise ValueError("matching temperature and error units required")
    if "standard deviation" not in attributes.get(("analysis_error", "long_name"), "").lower():
        raise ValueError("source error semantics unavailable")
    if (
        attributes.get(("latitude", "units")) != "degrees_north"
        or attributes.get(("longitude", "units")) != "degrees_east"
    ):
        raise ValueError("source coordinate units must be geographic degrees")
    if attributes.get(("mask", "flag_masks"), "").replace(
        " ", ""
    ) != "1,2,4,8,16" or attributes.get(("mask", "flag_meanings"), "").split() != [
        "open_sea",
        "land",
        "open_lake",
        "open_sea_with_ice_in_the_grid",
        "open_lake_with_ice_in_the_grid",
    ]:
        raise ValueError("source mask semantics are missing or changed")
    if "CF-" not in attributes.get(("NC_GLOBAL", "Conventions"), "") or attributes.get(
        ("time", "calendar"), "standard"
    ) not in {"standard", "gregorian", "proleptic_gregorian"}:
        raise ValueError("explicit supported CF calendar policy required")
    license_text = attributes.get(("NC_GLOBAL", "license"), "")
    if "may be used and redistributed for free" not in " ".join(license_text.split()):
        raise PermissionError("source preservation license not established")
    try:
        proof = MetadataProof(
            units=units,
            latitude_step=float(attributes["NC_GLOBAL", "geospatial_lat_resolution"]),
            longitude_step=float(attributes["NC_GLOBAL", "geospatial_lon_resolution"]),
            sst_min=float(attributes["analysed_sst", "valid_min"]),
            sst_max=float(attributes["analysed_sst", "valid_max"]),
            error_max=float(attributes["analysis_error", "valid_max"]),
        )
    except (KeyError, ValueError) as error:
        raise ValueError("native grid or source valid ranges unavailable") from error
    try:
        sst_fill = float(attributes["analysed_sst", "_FillValue"])
        error_fill = float(attributes["analysis_error", "_FillValue"])
        error_min = float(attributes["analysis_error", "valid_min"])
    except (KeyError, ValueError) as error:
        raise ValueError("explicit source fill and error-range metadata required") from error
    if (
        error_min != 0
        or proof.sst_min <= sst_fill <= proof.sst_max
        or 0 <= error_fill <= proof.error_max
    ):
        raise ValueError("source fill/range semantics differ from the declared native input policy")
    if (
        not all(
            math.isfinite(v)
            for v in (
                proof.latitude_step,
                proof.longitude_step,
                proof.sst_min,
                proof.sst_max,
                proof.error_max,
            )
        )
        or proof.sst_min >= proof.sst_max
        or proof.error_max <= 0
        or proof.latitude_step <= 0
        or proof.longitude_step <= 0
    ):
        raise ValueError("invalid source ranges")
    return proof


def parse_native_month(
    raw: bytes, year: int, month: int, grid: NativeGrid, metadata: MetadataProof
) -> NativeMonth:
    if not raw or len(raw) > 2 * 1024 * 1024 or year < 2000 or not 1 <= month <= 12:
        raise ValueError("bounded post-2000 month required")
    if not math.isclose(grid.latitude_step, metadata.latitude_step) or not math.isclose(
        grid.longitude_step, metadata.longitude_step
    ):
        raise ValueError("subsampling is not full native-grid support")
    rows = csv.reader(io.StringIO(raw.decode("utf-8")), strict=True)
    if next(rows, None) != [
        "time",
        "latitude",
        "longitude",
        "analysed_sst",
        "analysis_error",
        "mask",
    ]:
        raise ValueError("SST, QC and mask columns required")
    if next(rows, None) != [
        "UTC",
        "degrees_north",
        "degrees_east",
        metadata.units,
        metadata.units,
        "",
    ]:
        raise ValueError("source CSV units differ from pinned metadata")
    height, width = grid.shape()
    days = calendar.monthrange(year, month)[1]
    cells = []
    seen = set()
    nominal_time = None
    for row in rows:
        if len(row) != 6:
            raise ValueError("invalid source row shape")
        timestamp = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        if timestamp.utcoffset() != timedelta(0) or (timestamp.year, timestamp.month) != (
            year,
            month,
        ):
            raise ValueError("source season/year or UTC semantics differ from request")
        nominal = timestamp.strftime("%H:%M:%S.%f")
        if nominal_time is not None and nominal != nominal_time:
            raise ValueError("daily source nominal time changed within the month")
        nominal_time = nominal
        latitude, longitude, sst, error, mask_value = map(float, row[1:])
        if (
            not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not math.isfinite(mask_value)
            or not mask_value.is_integer()
        ):
            raise ValueError("invalid spatial coordinate or source mask")
        y, x = (
            round((latitude - grid.south) / grid.latitude_step),
            round((longitude - grid.west) / grid.longitude_step),
        )
        if (
            not 0 <= y < height
            or not 0 <= x < width
            or abs(latitude - (grid.south + y * grid.latitude_step)) > 5e-6
            or abs(longitude - (grid.west + x * grid.longitude_step)) > 5e-6
        ):
            raise ValueError("source grid falls outside requested native support")
        key = (timestamp.day, y, x)
        if key in seen:
            raise ValueError("duplicate daily source cell")
        seen.add(key)
        mask = int(mask_value)
        valid = (
            mask == 1
            and math.isfinite(sst)
            and math.isfinite(error)
            and metadata.sst_min <= sst <= metadata.sst_max
            and 0 <= error <= metadata.error_max
        )
        cells.append(SSTCell(timestamp.day, y, x, sst, error, mask, valid))
    if len(cells) != days * height * width:
        raise ValueError("incomplete month or missing native grid cells")
    if nominal_time is None:
        raise ValueError("source nominal time is unavailable")
    return NativeMonth(year, month, grid, tuple(cells), nominal_time)


def qualify_seasonal_inputs(
    current: NativeMonth, baseline: tuple[NativeMonth, ...], expected_years: tuple[int, ...]
) -> dict[str, object]:
    if (
        not expected_years
        or len(set(expected_years)) != len(expected_years)
        or len(baseline) != len(expected_years)
    ):
        raise ValueError("all distinct declared baseline years are required")
    if sorted(item.year for item in baseline) != sorted(expected_years):
        raise ValueError("baseline year selection differs from policy")
    for item in baseline:
        if (
            item.year >= current.year
            or item.month != current.month
            or item.grid != current.grid
            or item.nominal_time_utc != current.nominal_time_utc
        ):
            raise ValueError("baseline is future, seasonally mismatched or on a different grid")
        if len(item.cells) != len(current.cells):
            raise ValueError("baseline calendar support differs; explicit leap-day policy required")
    # The initial input benchmark is intentionally strict. Missing/masked cells
    # block admission rather than becoming an interpolated scientific result.
    if any(not cell.valid for item in (current, *baseline) for cell in item.cells):
        raise ValueError("native benchmark requires complete valid open-ocean QC support")
    height, width = current.grid.shape()
    return {
        "status": "INPUT_COMPATIBILITY_VERIFIED",
        "method_science_validated": False,
        "baseline_years": list(expected_years),
        "baseline_complete_years": len(baseline),
        "days_per_year": calendar.monthrange(current.year, current.month)[1],
        "native_latitudes": height,
        "native_longitudes": width,
        "current_cells": len(current.cells),
        "baseline_cells": sum(len(item.cells) for item in baseline),
        "valid_fraction": 1.0,
        "depth_policy": "same-product foundation SST; no fixed depth inferred",
        "uncertainty": "unknown aggregate covariance; no independent-error assumption",
    }


def qualify_availability(retrievals: tuple[datetime, ...], as_of: datetime) -> None:
    if not retrievals or as_of.utcoffset() is None:
        raise ValueError("explicit observed retrieval clocks and as-of time required")
    if any(when.utcoffset() is None or when > as_of for when in retrievals):
        raise ValueError("input was not observed available by the requested as-of time")
