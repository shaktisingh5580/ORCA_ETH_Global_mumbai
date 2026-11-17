"""Purpose: prove the preserved real NOAA batch's recorded as-of boundary.
Behavior: verify captured bytes and show current-time acceptance versus pre-retrieval rejection.
Refs: main implementation plan P03.4; ADR-R02; Phase: P03.4.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orca.domain.scientific_inputs import qualify_availability

pytestmark = pytest.mark.live


def test_real_as_of_availability():
    root = Path(__file__).resolve().parents[3]
    folder = root / ".cache/science-inputs/mur-native-batch"
    capture = json.loads((folder / "capture.json").read_text(encoding="utf-8"))
    assert capture["fixture"] is False
    input_records = list(capture["inputs"].values())
    metadata = capture["metadata"]
    records = input_records + [metadata]
    for record in records:
        raw = (
            (folder / record["file"]).read_bytes()
            if "file" in record
            else (folder / "metadata.json").read_bytes()
        )
        assert "sha256:" + hashlib.sha256(raw).hexdigest() == record["sha256"]
        assert len(raw) == record["bytes"]

    retrieved = tuple(datetime.fromisoformat(item["retrieved_at"]) for item in records)
    execution_cutoff = datetime.fromisoformat(capture["analysis_as_of"])
    qualify_availability(retrieved, execution_cutoff)
    historical_cutoff = datetime(2025, 2, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="not observed available"):
        qualify_availability(retrieved, historical_cutoff)
