"""Purpose: capture bounded live metadata evidence for user-supplied P03 SST candidates.
Behavior: use the hardened connector and record hashes, actual attributes and honest failures.
Refs: user Exact Data Endpoints attachment; main plan P03.4; Phase: P03.4.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from orca.contracts.erddap_metadata import parse_metadata
from orca.infrastructure.connectors.erddap import ERDDAP

ROOT = Path(__file__).resolve().parents[2]
CANDIDATES = (
    ("erddap.incois.gov.in", "NOAA_AVHRR_AMSR_datasets"),
    ("coastwatch.pfeg.noaa.gov", "jplMURSST41"),
)


def probe(host: str, product: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "host": host,
        "product_id": product,
        "metadata_url": f"https://{host}/erddap/info/{product}/index.json",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "fixture": False,
    }
    try:
        client = ERDDAP(host, frozenset(h for h, _ in CANDIDATES))
        raw = client._get(f"/erddap/info/{product}/index.json")
        cache = ROOT / ".cache/source-probes" / product
        cache.mkdir(parents=True, exist_ok=True)
        cache.joinpath("metadata.json").write_bytes(raw)
        document = json.loads(raw)
        variables = parse_metadata(document)
        result["response_sha256"] = hashlib.sha256(raw).hexdigest()
        result["response_bytes"] = len(raw)
        result["variables"] = [
            {
                "name": v.name,
                "dimensions": v.dimensions,
                "units": v.native_units,
                "standard_name": v.standard_name,
            }
            for v in variables
        ]
        result["attributes"] = [row for row in document["table"]["rows"] if row[0] == "attribute"]
        result["status"] = "METADATA_RETRIEVED"
        if product == "jplMURSST41":
            # Candidate access sample only: spatial subsampling is not the final scientific input.
            slices = "[(2025-01-01T09:00:00Z):1:(2025-01-31T09:00:00Z)][(20):10:(21)][(68):10:(69)]"
            query = ",".join(name + slices for name in ("analysed_sst", "analysis_error", "mask"))
            path = f"/erddap/griddap/{product}.csv?{quote(query, safe=':,')}"
            subset = client._get(path)
            if not subset.startswith(b"time,latitude,longitude,"):
                raise ValueError("unexpected subset header")
            cache.joinpath("january-2025-access-sample.csv").write_bytes(subset)
            result["access_sample"] = {
                "url": f"https://{host}{path}",
                "sha256": hashlib.sha256(subset).hexdigest(),
                "bytes": len(subset),
                "local_path": cache.relative_to(ROOT).as_posix()
                + "/january-2025-access-sample.csv",
                "scientific_validation": "NOT_VALIDATED",
                "spatial_stride": 10,
            }
    except Exception as exc:
        result["status"] = "BLOCKED"
        result["error_type"] = type(exc).__name__
        result["error_code"] = getattr(exc, "code", "SOURCE_PROBE_FAILED")
        result["http_status"] = getattr(exc, "status", None)
    return result


def main() -> None:
    reports = [probe(host, product) for host, product in CANDIDATES]
    output = ROOT / "docs/gates/P03/source-probes.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "phase": "P03.4",
                "gate_status": "BLOCKED",
                "reason": "Metadata probes do not establish subset, QC, baseline compatibility or rights acceptance.",
                "probes": reports,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    for report in reports:
        print(json.dumps({k: v for k, v in report.items() if k not in {"attributes", "variables"}}))


if __name__ == "__main__":
    main()
