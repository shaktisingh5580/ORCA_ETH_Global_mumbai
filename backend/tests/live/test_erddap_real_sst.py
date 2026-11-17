"""Purpose: verify fresh NOAA reads and the preserved real MUR seasonal baseline.
Behavior: qualify bounded live responses and reparse exact preserved source bytes without fixtures.
Refs: main implementation plan P03.4; ADR-R02; Phase: P03.
"""

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import quote

import pytest
from acquire_scientific_inputs import BASELINE_YEARS, CURRENT_YEAR, DATASET, HOST, fetch, query_for

from orca.domain.scientific_inputs import (
    NativeGrid,
    parse_native_month,
    qualify_metadata,
    qualify_seasonal_inputs,
)
from orca.infrastructure.connectors.erddap import ERDDAP, SourceError

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def source():
    if os.environ.get("ORCA_TEST_LIVE_MUR") != "1":
        pytest.skip("Set ORCA_TEST_LIVE_MUR=1 to retrieve public NOAA data")
    return ERDDAP(HOST, frozenset({HOST}), max_bytes=2 * 1024 * 1024, timeout=30)


def read(source, path):
    try:
        return fetch(source, path, attempts=2)
    except SourceError as error:
        pytest.fail(
            f"NOAA retrieval failed: {error.code}; HTTP status {error.status}", pytrace=False
        )


@pytest.fixture(scope="module")
def metadata(source):
    raw = read(source, f"/erddap/info/{DATASET}/index.json")
    return raw, qualify_metadata(json.loads(raw))


@pytest.mark.enable_socket
def test_live_product_metadata_access(metadata, record_property):
    raw, proof = metadata
    assert proof.latitude_step == proof.longitude_step == 0.01
    record_property("metadata_sha256", hashlib.sha256(raw).hexdigest())


@pytest.mark.enable_socket
def test_live_subset_bytes_and_qc(source, metadata, record_property):
    _, proof = metadata
    query = quote(query_for(CURRENT_YEAR), safe=":,")
    raw = read(source, f"/erddap/griddap/{DATASET}.csv?{query}")
    grid = NativeGrid(68.45, 20.45, 68.55, 20.55, proof.longitude_step, proof.latitude_step)
    month = parse_native_month(raw, CURRENT_YEAR, 1, grid, proof)
    assert len(month.cells) == 31 * 11 * 11
    assert all(cell.valid for cell in month.cells)
    record_property("subset_sha256", hashlib.sha256(raw).hexdigest())
    record_property("subset_bytes", len(raw))


def test_preserved_real_mur_baseline_qualification(record_property):
    root = Path(__file__).resolve().parents[3]
    folder = root / ".cache/science-inputs/mur-native-batch"
    capture = json.loads((folder / "capture.json").read_text(encoding="utf-8"))
    assert capture["source_host"] == HOST
    assert capture["dataset"] == DATASET
    assert capture["fixture"] is False

    raw_metadata = (folder / "metadata.json").read_bytes()
    metadata_digest = "sha256:" + hashlib.sha256(raw_metadata).hexdigest()
    assert metadata_digest == capture["metadata"]["sha256"]
    proof = qualify_metadata(json.loads(raw_metadata))
    assert proof.latitude_step == proof.longitude_step == 0.01
    grid = NativeGrid(68.45, 20.45, 68.55, 20.55, proof.longitude_step, proof.latitude_step)
    record_property("preserved_metadata_sha256", metadata_digest)

    months = {}
    for year in (CURRENT_YEAR, *BASELINE_YEARS):
        record = capture["inputs"][str(year)]
        raw = (folder / record["file"]).read_bytes()
        assert "sha256:" + hashlib.sha256(raw).hexdigest() == record["sha256"]
        assert len(raw) == record["bytes"]
        assert record["query"] == query_for(year)
        months[year] = parse_native_month(raw, year, 1, grid, proof)
        record_property(f"preserved_mur_{year}_sha256", record["sha256"])

    qualification = qualify_seasonal_inputs(
        months[CURRENT_YEAR], tuple(months[year] for year in BASELINE_YEARS), BASELINE_YEARS
    )
    assert qualification == capture["qualification"]
    assert qualification["status"] == "INPUT_COMPATIBILITY_VERIFIED"
    assert qualification["baseline_complete_years"] == 20
    assert qualification["days_per_year"] == 31
    assert qualification["current_cells"] == 3751
    assert qualification["baseline_cells"] == 75020
    assert qualification["valid_fraction"] == 1.0
    assert qualification["method_science_validated"] is False
    record_property("preserved_mur_qualification", json.dumps(qualification, sort_keys=True))
