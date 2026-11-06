"""Purpose: capture the bounded real MUR current/baseline inputs declared in ADR-R02.
Behavior: resume hash-checked captures, bound retries, honor backoff and fail closed on missing input.
Refs: ADR-R02; main plan P03.4; primary NOAA MUR metadata; Phase: P03.
"""

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from orca.domain.scientific_inputs import (
    NativeGrid,
    parse_native_month,
    qualify_availability,
    qualify_metadata,
    qualify_seasonal_inputs,
)
from orca.infrastructure.connectors.erddap import ERDDAP, SourceError

ROOT = Path(__file__).resolve().parents[2]
HOST = "coastwatch.pfeg.noaa.gov"
DATASET = "jplMURSST41"
BASELINE_YEARS = tuple(range(2003, 2023))
CURRENT_YEAR = 2025


def digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def query_for(year: int) -> str:
    slices = f"[({year}-01-01T09:00:00Z):1:({year}-01-31T09:00:00Z)][(20.45):1:(20.55)][(68.45):1:(68.55)]"
    return ",".join(name + slices for name in ("analysed_sst", "analysis_error", "mask"))


def fetch(client: ERDDAP, path: str, attempts: int) -> bytes:
    for attempt in range(attempts):
        try:
            return client._get(path)
        except SourceError as error:
            if attempt + 1 >= attempts or (
                error.status not in {429, 500, 502, 503, 504}
                and error.code not in {"SOURCE_TRANSPORT_ERROR", "SOURCE_TIMEOUT"}
            ):
                raise
            delay = 2
            if error.retry_after:
                if not error.retry_after.isdecimal() or int(error.retry_after) > 10:
                    raise
                delay = max(1, int(error.retry_after))
            print(
                json.dumps(
                    {
                        "status": "RETRY_SCHEDULED",
                        "http_status": error.status,
                        "delay_seconds": delay,
                    }
                ),
                flush=True,
            )
            time.sleep(delay)
    raise RuntimeError("bounded fetch exhausted")


def write(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cache-dir", type=Path, default=ROOT / ".cache/science-inputs/mur-native-batch"
    )
    parser.add_argument("--attempts", type=int, choices=(1, 2, 3), default=2)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    folder = args.cache_dir.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    journal = folder / "capture.json"
    report: dict[str, Any] = (
        json.loads(journal.read_text(encoding="utf-8"))
        if journal.exists()
        else {
            "phase": "P03.4",
            "source_host": HOST,
            "dataset": DATASET,
            "fixture": False,
            "policy": "ADR-R02",
            "inputs": {},
            "qualification": "INCOMPLETE",
        }
    )
    if (
        report.get("source_host") != HOST
        or report.get("dataset") != DATASET
        or report.get("fixture") is not False
    ):
        raise ValueError("capture journal does not match approved real source")
    client = ERDDAP(HOST, frozenset({HOST}), max_bytes=2 * 1024 * 1024, timeout=30)
    try:
        metadata_path = folder / "metadata.json"
        if "metadata" not in report:
            if args.offline:
                raise ValueError("real metadata capture unavailable offline")
            raw = fetch(client, f"/erddap/info/{DATASET}/index.json", args.attempts)
            qualify_metadata(json.loads(raw))
            metadata_path.write_bytes(raw)
            report["metadata"] = {
                "sha256": digest(raw),
                "bytes": len(raw),
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "url": f"https://{HOST}/erddap/info/{DATASET}/index.json",
            }
            write(journal, report)
        metadata_bytes = metadata_path.read_bytes()
        if digest(metadata_bytes) != report["metadata"]["sha256"]:
            raise ValueError("preserved metadata checksum mismatch")
        metadata = qualify_metadata(json.loads(metadata_bytes))
        grid = NativeGrid(
            68.45, 20.45, 68.55, 20.55, metadata.longitude_step, metadata.latitude_step
        )
        parsed = {}
        for year in (CURRENT_YEAR, *BASELINE_YEARS):
            query = query_for(year)
            source_path = f"/erddap/griddap/{DATASET}.csv?{quote(query, safe=':,')}"
            target = folder / f"{year}-01.csv"
            record = report["inputs"].get(str(year))
            if record is None:
                if args.offline:
                    raise ValueError("declared baseline/current capture unavailable offline")
                raw = fetch(client, source_path, args.attempts)
                parse_native_month(raw, year, 1, grid, metadata)
                target.write_bytes(raw)
                record = {
                    "year": year,
                    "month": 1,
                    "sha256": digest(raw),
                    "bytes": len(raw),
                    "query": query,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "file": target.name,
                }
                report["inputs"][str(year)] = record
                write(journal, report)
                print(
                    json.dumps({"year": year, "status": "RETRIEVED", "bytes": len(raw)}), flush=True
                )
            raw = target.read_bytes()
            if (
                digest(raw) != record["sha256"]
                or len(raw) != record["bytes"]
                or record["query"] != query
            ):
                raise ValueError("preserved source content or query differs from capture journal")
            parsed[year] = parse_native_month(raw, year, 1, grid, metadata)
        qualification = qualify_seasonal_inputs(
            parsed[CURRENT_YEAR], tuple(parsed[y] for y in BASELINE_YEARS), BASELINE_YEARS
        )
        report["qualification"] = qualification
        as_of = datetime.now(timezone.utc)
        qualify_availability(
            tuple(
                datetime.fromisoformat(value["retrieved_at"]) for value in report["inputs"].values()
            )
            + (datetime.fromisoformat(report["metadata"]["retrieved_at"]),),
            as_of,
        )
        report["analysis_as_of"] = as_of.isoformat()
        report["availability_policy"] = (
            "First observed at actual retrieval; historical 2025 availability is not asserted"
        )
        report.pop("block", None)
        write(journal, report)
        print(json.dumps(qualification), flush=True)
        return 0
    except (SourceError, ValueError, PermissionError) as error:
        report["qualification"] = "BLOCKED"
        report["block"] = {
            "code": getattr(error, "code", str(error)),
            "http_status": getattr(error, "status", None),
            "retry_after": getattr(error, "retry_after", None),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        write(journal, report)
        print(json.dumps(report["block"]), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
