"""Purpose: normalize bounded CSV/JSON observations without model-generated calculations.
Behavior: preserve row provenance, reject ambiguity and account for every input row.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md sections 3-4; Phase: A01.
"""

import csv
import hashlib
import io
import json
import math
from datetime import datetime, timezone
from typing import Any

from orca.contracts.normalization import Mapping

MAX_ROWS = 10_000
MAX_BYTES = 5_242_880


def digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def parse(content: str, format: str) -> list[dict[str, Any]]:
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Input exceeds 5 MiB")
    if format == "csv":
        reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")), strict=True)
        fields = reader.fieldnames or []
        if not fields or len(set(fields)) != len(fields) or any(not f for f in fields):
            raise ValueError("CSV requires unique nonempty column names")
        rows: list[dict[str, Any]] = []
        for row in reader:
            if None in row or any(v is None for v in row.values()):
                raise ValueError("CSV row does not match header width")
            rows.append(dict(row))
            if len(rows) > MAX_ROWS:
                raise ValueError("Input exceeds 10000 observations")
    elif format == "json":

        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in items:
                if key in result:
                    raise ValueError("Duplicate JSON property")
                result[key] = value
            return result

        def invalid_constant(value: str) -> None:
            raise ValueError("Non-finite JSON number")

        decoded = json.loads(content, object_pairs_hook=pairs, parse_constant=invalid_constant)
        if not isinstance(decoded, list):
            raise ValueError("JSON must be an array of flat objects")
        rows = decoded
    else:
        raise ValueError("Unsupported format")
    if not rows or len(rows) > MAX_ROWS:
        raise ValueError("Input requires 1 to 10000 observations")
    for row in rows:
        if not isinstance(row, dict) or not row or len(row) > 64:
            raise ValueError("Each observation must be an object with 1 to 64 fields")
        if any(not isinstance(k, str) or not k or len(k) > 100 for k in row):
            raise ValueError("Invalid column name")
        if any(isinstance(v, (dict, list)) for v in row.values()):
            raise ValueError("Nested JSON is unsupported")
    return rows


def inspect(content: str, format: str) -> dict[str, Any]:
    rows = parse(content, format)
    columns = sorted({key for row in rows for key in row})
    return {
        "row_count": len(rows),
        "columns": columns,
        "preview": rows[:5],
        "missing": {k: sum(r.get(k) in (None, "") for r in rows) for k in columns},
        "requires_explicit_unit": True,
    }


def number(value: Any) -> float:
    if isinstance(value, bool) or value is None or value == "":
        raise ValueError("missing or invalid numeric value")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite numeric value")
    return result


def normalize(content: str, format: str, mapping: Mapping) -> dict[str, Any]:
    rows = parse(content, format)
    columns = {k for row in rows for k in row}
    selected = [
        mapping.timestamp_column,
        mapping.value_column,
        mapping.id_column,
        mapping.latitude_column,
        mapping.longitude_column,
    ]
    if any(c is not None and c not in columns for c in selected):
        raise ValueError("Mapping references an unknown column")
    if bool(mapping.latitude_column) != bool(mapping.longitude_column):
        raise ValueError("Map both coordinates or neither")
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    duplicates: list[int] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, 1):
        try:
            stamp = datetime.fromisoformat(
                str(row.get(mapping.timestamp_column, "")).replace("Z", "+00:00")
            )
            if stamp.tzinfo is None:
                if mapping.naive_timezone != "UTC":
                    raise ValueError("timestamp requires offset or approved UTC declaration")
                stamp = stamp.replace(tzinfo=timezone.utc)
            timestamp = stamp.astimezone(timezone.utc).isoformat()
            value = number(row.get(mapping.value_column))
            if mapping.unit == "K":
                if value < 0:
                    raise ValueError("Kelvin value below absolute zero")
                value -= 273.15
            if value < -273.15:
                raise ValueError("temperature below absolute zero")
            latitude = number(row.get(mapping.latitude_column)) if mapping.latitude_column else None
            longitude = (
                number(row.get(mapping.longitude_column)) if mapping.longitude_column else None
            )
            if latitude is not None and not -90 <= latitude <= 90:
                raise ValueError("latitude out of range")
            if longitude is not None and not -180 <= longitude <= 180:
                raise ValueError("longitude out of range")
            identity = (
                str(row.get(mapping.id_column, ""))
                if mapping.id_column
                else digest([timestamp, latitude, longitude])
            )
            if not identity or identity == "None":
                raise ValueError("missing observation identifier")
            if identity in seen:
                if mapping.duplicate_policy == "keep_first":
                    duplicates.append(index)
                    continue
                raise ValueError("duplicate observation identifier")
            seen.add(identity)
            accepted.append(
                {
                    "observation_id": identity,
                    "timestamp_utc": timestamp,
                    "latitude": latitude,
                    "longitude": longitude,
                    "variable": mapping.variable,
                    "value": round(value, 10),
                    "unit": "C",
                    "source_row_id": index,
                }
            )
        except (ValueError, TypeError, OverflowError) as error:
            rejected.append({"source_row_id": index, "reason": str(error)[:160]})
    accepted.sort(key=lambda r: (r["timestamp_utc"], r["observation_id"]))
    return {
        "schema_version": "orca.observations/1",
        "input_rows": len(rows),
        "accepted_rows": len(accepted),
        "rejected_rows": rejected,
        "duplicate_rows_skipped": duplicates,
        "observations": accepted,
        "ready_for_analysis": bool(accepted) and not rejected,
        "serialization": "sorted-json/1; Celsius rounded to 10 decimal places",
    }
