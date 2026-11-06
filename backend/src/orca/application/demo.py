"""Purpose: compute a reproducible descriptive SST demo from verified preserved CSV bytes.
Behavior: retain native measurement semantics, convert units explicitly and avoid anomaly claims.
Refs: scientific engine 7; accelerated-research-arkiv-demo.json; Phase: accelerated P04 demo.
"""

import csv
import hashlib
import io
import math
from collections import defaultdict
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from orca.contracts.demo import DemoResult
from orca.contracts.snapshots import DatasetSnapshotManifest


def result_digest(result: DemoResult) -> str:
    import json

    canonical = json.dumps(
        result.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def analyze_snapshot(manifest: DatasetSnapshotManifest, content: bytes) -> DemoResult:
    if not content or len(content) > 8_388_608 or len(content) != manifest.byte_count:
        raise ValueError("source size mismatch or exceeds demo limit")
    if "sha256:" + hashlib.sha256(content).hexdigest() != manifest.raw_content_digest:
        raise ValueError("source digest mismatch")
    version = manifest.product_version
    rows = csv.reader(io.StringIO(content.decode("iso-8859-1")))
    columns, units = next(rows, []), next(rows, [])
    if (
        columns[: len(version.dimensions)] != list(version.dimensions)
        or version.native_variable not in columns
        or len(set(columns)) != len(columns)
        or len(units) != len(columns)
    ):
        raise ValueError("demo requires preserved ERDDAP CSV with pinned dimensions")
    variable_index = columns.index(version.native_variable)
    if "time" not in columns or units[variable_index] != version.native_units:
        raise ValueError("source time or units mismatch")
    values: list[float] = []
    daily: dict[str, list[float]] = defaultdict(list)
    row_count = 0
    for row in rows:
        row_count += 1
        if row_count > 100_000 or len(row) != len(columns):
            raise ValueError("malformed or oversized source table")
        value = float(row[variable_index])
        stamp = datetime.fromisoformat(row[columns.index("time")].replace("Z", "+00:00"))
        if stamp.tzinfo is None or math.isinf(value):
            raise ValueError("invalid observation")
        if math.isnan(value):
            continue
        celsius = value - 273.15 if version.native_units == "K" else value
        values.append(celsius)
        daily[stamp.date().isoformat()].append(celsius)
    if not values:
        raise ValueError("no finite observations")
    return DemoResult(
        id=uuid5(
            NAMESPACE_URL, f"orca:demo:{manifest.tenant_id}:{manifest.id}:descriptive-sst/1.0"
        ),
        snapshot_id=manifest.id,
        source_digest=manifest.raw_content_digest,
        row_count=row_count,
        finite_count=len(values),
        minimum=min(values),
        maximum=max(values),
        sample_mean=math.fsum(values) / len(values),
        daily_sample_means={
            day: math.fsum(samples) / len(samples) for day, samples in sorted(daily.items())
        },
        measurement_definition=version.physical_definition,
        limitations=[
            "Descriptive sample statistics; not an area-weighted regional mean.",
            "QC and valid ocean/temporal coverage have not been validated.",
            "No compatible baseline is applied; this is not an SST anomaly.",
            "No calibrated uncertainty or causal conclusion is asserted.",
        ],
    )
