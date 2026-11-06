"""Purpose: fetch bounded native SST CSV bytes for approved pinned ERDDAP versions.
Behavior: verify metadata identity and response coordinates, units and shape before preservation.
Refs: main plan P03.2/P03.3; ERDDAP griddap documentation; Phase: P03.
"""

import csv
import hashlib
import io
import json
import math
from datetime import datetime, timedelta
from urllib.parse import quote

from shapely.geometry import box, shape

from orca.application.acquisition_worker import AcquisitionFailed, AcquisitionResult
from orca.contracts.acquisition import SourceBinding
from orca.contracts.catalog import ProductVersion
from orca.contracts.erddap_metadata import parse_metadata
from orca.contracts.snapshot_requests import SnapshotRequest, request_digest
from orca.infrastructure.connectors.erddap import ERDDAP, SourceError


class ERDDAPAcquisition:
    def __init__(self, binding: SourceBinding):
        self.binding = binding
        self.client = ERDDAP(binding.host, frozenset({binding.host}), max_bytes=8_388_608)

    def fetch(self, request: SnapshotRequest, version: ProductVersion) -> AcquisitionResult:
        try:
            return self._fetch(request, version)
        except (SourceError, ValueError, KeyError, TypeError, csv.Error) as exc:
            raise AcquisitionFailed("SOURCE_VALIDATION_FAILED") from exc

    def _fetch(self, request: SnapshotRequest, version: ProductVersion) -> AcquisitionResult:
        if (
            version.id != self.binding.version_id
            or version.tenant_id != self.binding.tenant_id
            or request.product_version_id != version.id
            or request.variables != [version.native_variable]
            or not version.can_preserve_for(self.binding.tenant_id)
        ):
            raise ValueError("unapproved source binding")
        if version.calendar not in {"gregorian", "standard", "proleptic_gregorian"}:
            raise ValueError("unsupported calendar")
        if (
            request.time_window.calendar not in {None, version.calendar}
            or request.time_window.temporal_resolution is not None
        ):
            raise ValueError("unsupported temporal transformation")
        if request.depth is not None and request.depth != version.depth_m:
            raise ValueError("depth mismatch")
        geometry = shape(request.geo_scope.geometry.model_dump())
        west, south, east, north = geometry.bounds
        if request.geo_scope.type != "Point" and not geometry.equals(box(west, south, east, north)):
            raise ValueError("only points and rectangular regions supported")
        if east - west > 2 or north - south > 2:
            raise ValueError("region exceeds bounded acquisition area")
        dataset = self.binding.dataset
        raw = self.client._get(f"/erddap/info/{dataset}/index.json")
        if "sha256:" + hashlib.sha256(raw).hexdigest() != version.metadata_digest:
            raise ValueError("metadata changed; register a new pinned version")
        document = json.loads(raw)
        variables = {item.name: item for item in parse_metadata(document)}
        native = variables[version.native_variable]
        if (
            native.dimensions != version.dimensions
            or native.native_units != version.native_units
            or native.standard_name != version.standard_name
        ):
            raise ValueError("scientific metadata mismatch")
        dims = version.dimensions
        if len(dims) != 3 or set(dims) != {"time", "latitude", "longitude"}:
            raise ValueError("only explicit surface time/latitude/longitude grids supported")
        attributes = {(r[1], r[2]): r[4] for r in document["table"]["rows"] if r[0] == "attribute"}
        if (
            attributes.get(("latitude", "units")) != "degrees_north"
            or attributes.get(("longitude", "units")) != "degrees_east"
        ):
            raise ValueError("coordinate units mismatch")
        window = request.time_window
        start = window.start.isoformat().replace("+00:00", "Z")
        end = (window.end - timedelta(microseconds=1)).isoformat().replace("+00:00", "Z")
        slices = {
            "time": f"[({start}):1:({end})]",
            "latitude": f"[({south}):1:({north})]",
            "longitude": f"[({west}):1:({east})]",
        }
        query = self.client._identifier(version.native_variable) + "".join(slices[d] for d in dims)
        content = self.client._get(f"/erddap/griddap/{dataset}.csv?{quote(query, safe=':')}")
        rows = csv.reader(io.StringIO(content.decode("iso-8859-1")))
        if next(rows, None) != [*dims, version.native_variable]:
            raise ValueError("response columns mismatch")
        units = {"time": "UTC", "latitude": "degrees_north", "longitude": "degrees_east"}
        if next(rows, None) != [*(units[d] for d in dims), version.native_units]:
            raise ValueError("response units mismatch")
        seen: set[tuple[str, ...]] = set()
        axes: dict[str, set[str]] = {d: set() for d in dims}
        finite = 0
        for row in rows:
            if len(row) != 4 or len(seen) >= 100_000:
                raise ValueError("malformed or oversized grid")
            coordinates = dict(zip(dims, row[:3], strict=True))
            stamp = datetime.fromisoformat(coordinates["time"].replace("Z", "+00:00"))
            lat, lon = float(coordinates["latitude"]), float(coordinates["longitude"])
            if (
                stamp.tzinfo is None
                or not window.start <= stamp < window.end
                or not south <= lat <= north
                or not west <= lon <= east
            ):
                raise ValueError("source rounded outside requested scope")
            value = float(row[3])
            if math.isinf(value):
                raise ValueError("infinite data value")
            finite += int(math.isfinite(value))
            key = (stamp.isoformat(), str(lat), str(lon))
            if key in seen:
                raise ValueError("duplicate coordinate")
            seen.add(key)
            for dimension, coordinate in coordinates.items():
                axes[dimension].add(coordinate)
        if not finite or len(seen) != math.prod(len(axis) for axis in axes.values()):
            raise ValueError("empty or incomplete response grid")
        return AcquisitionResult(
            content,
            f"https://{self.binding.host}/erddap/griddap/{dataset}.csv",
            request_digest(request),
        )
