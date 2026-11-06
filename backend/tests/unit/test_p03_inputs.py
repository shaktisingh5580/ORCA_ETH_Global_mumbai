"""Purpose: test P03 metadata interpretation and atomic exact-byte preservation.
Behavior: exercise malformed metadata, rights denial, deduplication and corruption.
Refs: main plan P03.2/P03.3; research-only guide 5; Phase: P03.
"""

import hashlib
from uuid import uuid4

import pytest

from orca.contracts.erddap_metadata import parse_metadata
from orca.infrastructure.storage.snapshot_files import SnapshotFiles


def test_metadata_dimension_variable_mapping():
    document = {
        "table": {
            "columnNames": ["Row Type", "Variable Name", "Attribute Name", "Data Type", "Value"],
            "rows": [
                ["variable", "sst", "", "float", "time, latitude, longitude"],
                ["attribute", "sst", "units", "String", "Degree C"],
                ["attribute", "sst", "standard_name", "String", "sea_surface_temperature"],
            ],
        }
    }
    (variable,) = parse_metadata(document)
    assert variable.dimensions == ("time", "latitude", "longitude")
    assert variable.native_units == "Degree C"
    with pytest.raises(ValueError):
        parse_metadata({"table": {"rows": []}})


def test_sha256_exact_bytes(tmp_path):
    store = SnapshotFiles(tmp_path)
    tenant = uuid4()
    content = b"test-only\r\nraw\x00bytes"
    digest = store.preserve(tenant, content, permitted=True)
    assert digest == "sha256:" + hashlib.sha256(content).hexdigest()
    assert (tmp_path / str(tenant) / digest[7:]).read_bytes() == content


def test_duplicate_retrieval_versioned(tmp_path):
    store, tenant = SnapshotFiles(tmp_path), uuid4()
    first = store.preserve(tenant, b"first", permitted=True)
    assert store.preserve(tenant, b"first", permitted=True) == first
    assert store.preserve(tenant, b"changed", permitted=True) != first
    (tmp_path / str(tenant) / first[7:]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="corrupt"):
        store.preserve(tenant, b"first", permitted=True)


def test_no_partial_publish(tmp_path):
    store, tenant = SnapshotFiles(tmp_path, max_bytes=4), uuid4()
    with pytest.raises(PermissionError):
        store.preserve(tenant, b"data", permitted=False)
    with pytest.raises(ValueError):
        store.preserve(tenant, b"oversized", permitted=True)
    assert list(tmp_path.iterdir()) == []
