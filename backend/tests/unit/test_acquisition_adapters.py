"""Purpose: verify scientific source rejection and cloud byte integrity at adapter boundaries.
Behavior: inject test-only HTTP/S3 responses; never use synthetic bytes in runtime composition.
Refs: main plan P03.2/P03.3; API 7; Phase: P03.
"""

import hashlib
import io
import json
from unittest.mock import Mock
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from orca.application.acquisition_worker import AcquisitionFailed
from orca.config import Settings
from orca.contracts.acquisition import SourceBinding
from orca.contracts.catalog import ProductVersion
from orca.contracts.snapshot_requests import SnapshotRequest, request_digest
from orca.infrastructure.connectors.acquisition import ERDDAPAcquisition
from orca.infrastructure.storage.snapshot_s3 import SnapshotS3


def source_case():
    metadata = json.dumps(
        {
            "table": {
                "columnNames": [
                    "Row Type",
                    "Variable Name",
                    "Attribute Name",
                    "Data Type",
                    "Value",
                ],
                "rows": [
                    ["variable", "sst", "", "float", "time, latitude, longitude"],
                    ["attribute", "sst", "units", "String", "degree_C"],
                    [
                        "attribute",
                        "sst",
                        "standard_name",
                        "String",
                        "sea_surface_foundation_temperature",
                    ],
                    ["attribute", "latitude", "units", "String", "degrees_north"],
                    ["attribute", "longitude", "units", "String", "degrees_east"],
                ],
            }
        }
    ).encode()
    version = ProductVersion(
        id=uuid4(),
        tenant_id=uuid4(),
        product_id=uuid4(),
        source_version="test",
        metadata_digest="sha256:" + hashlib.sha256(metadata).hexdigest(),
        native_variable="sst",
        standard_name="sea_surface_foundation_temperature",
        physical_definition="test foundation SST",
        native_units="degree_C",
        dimensions=("time", "latitude", "longitude"),
        calendar="gregorian",
        retrieved_at="2025-02-01T00:00:00Z",
        license={
            "license_ref": "test",
            "evidence_digest": "sha256:" + "a" * 64,
            "can_preserve": True,
        },
    )
    body = SnapshotRequest(
        organization_id=uuid4(),
        product_version_id=version.id,
        geo_scope={"type": "Point", "geometry": {"type": "Point", "coordinates": [68.5, 20.5]}},
        time_window={"start": "2025-01-01T00:00:00Z", "end": "2025-01-02T00:00:00Z"},
        variables=["sst"],
    )
    source = ERDDAPAcquisition(
        SourceBinding(
            tenant_id=version.tenant_id,
            version_id=version.id,
            host="coastwatch.pfeg.noaa.gov",
            dataset="test",
        )
    )
    content = b"time,latitude,longitude,sst\nUTC,degrees_north,degrees_east,degree_C\n2025-01-01T09:00:00Z,20.5,68.5,25.5\n"
    source.client._get = Mock(side_effect=[metadata, content])
    return source, body, version, metadata, content


def test_source_preserves_exact_validated_native_bytes():
    source, body, version, _, content = source_case()
    result = source.fetch(body, version)
    assert result.content == content and result.request_digest == request_digest(body)
    assert "?" not in result.origin_uri
    assert ".csv?sst" in source.client._get.call_args.args[0]


@pytest.mark.parametrize(
    "change", ["metadata", "units", "location", "time", "empty", "duplicate", "infinite"]
)
def test_source_rejects_malformed_or_unbound_responses(change):
    source, body, version, metadata, content = source_case()
    if change == "metadata":
        metadata += b" "
    elif change == "units":
        content = content.replace(b"degree_C", b"K")
    elif change == "location":
        content = content.replace(b"68.5", b"68.6")
    elif change == "time":
        content = content.replace(b"01-01T09", b"01-02T09")
    elif change == "empty":
        content = content.replace(b"25.5", b"NaN")
    elif change == "infinite":
        content = content.replace(b"25.5", b"inf")
    else:
        content += content.splitlines()[-1] + b"\n"
    source.client._get = Mock(side_effect=[metadata, content])
    with pytest.raises(AcquisitionFailed):
        source.fetch(body, version)


def test_cloud_snapshot_readback_and_corruption_rejection():
    client = Mock()
    tenant = uuid4()
    content = b"test bytes\r\n"
    missing = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    client.get_object.side_effect = [
        missing,
        {"Body": io.BytesIO(content), "ContentLength": len(content)},
    ]
    store = SnapshotS3(client, "test")
    digest = store.preserve(tenant, content, permitted=True)
    assert digest == "sha256:" + hashlib.sha256(content).hexdigest()
    assert client.put_object.call_args.kwargs["Key"].startswith(f"snapshots/{tenant}/")
    client.get_object.side_effect = [{"Body": io.BytesIO(b"corrupt"), "ContentLength": 7}]
    with pytest.raises(ValueError, match="corrupt"):
        store.preserve(tenant, content, permitted=True)
    assert client.put_object.call_count == 1
    with pytest.raises(PermissionError):
        store.preserve(tenant, content, permitted=False)


def test_acquisition_configuration_fails_closed():
    with pytest.raises(ValueError, match="acquisition requires"):
        Settings(monitoring_token="0123456789abcdefghijklmnopqrstuvwxyz", acquisition_enabled=True)
