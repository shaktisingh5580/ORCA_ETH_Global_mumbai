"""Purpose: verify snapshot publication preserves rights, identity and exact-byte provenance.
Behavior: inject test storage and exercise rejection before writes and truthful unknown fields.
Refs: main plan P03.3; research-only guide 5.2; Phase: P03.3.
"""

import hashlib
from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from orca.application.ingestion import preserve_snapshot
from orca.contracts.catalog import ProductVersion


def version():
    return ProductVersion(
        id=uuid4(),
        tenant_id=uuid4(),
        product_id=uuid4(),
        source_version="test",
        metadata_digest="sha256:" + "a" * 64,
        native_variable="sst",
        standard_name="sea_surface_temperature",
        physical_definition="test-only SST",
        native_units="K",
        calendar="gregorian",
        retrieved_at="2025-02-01T00:00:00Z",
        license={
            "license_ref": "test-license",
            "evidence_digest": "sha256:" + "b" * 64,
            "can_preserve": True,
        },
    )


def test_snapshot_exact_bytes_and_unknown_evidence():
    v, store, content = version(), Mock(), b"test\r\nbytes"
    store.preserve.return_value = "sha256:" + hashlib.sha256(content).hexdigest()
    manifest = preserve_snapshot(
        store,
        v.tenant_id,
        v,
        content,
        origin_uri="https://example.org/data.nc",
        retrieved_at=datetime.now(timezone.utc),
    )
    assert manifest.byte_count == len(content)
    assert manifest.product_version.available_at is None
    assert manifest.scientific_status == "not_validated" and manifest.quality_policy_ref is None
    store.preserve.assert_called_once_with(v.tenant_id, content, permitted=True)
    with pytest.raises(ValidationError):
        manifest.raw_content_digest = "sha256:" + "c" * 64


def test_snapshot_rejects_before_storage():
    v, store = version(), Mock()
    for uri in (
        "http://example.org/data",
        "https://user:secret@example.org/data",
        "https://example.org/data?token=secret",
        "https://example.org/data#secret",
    ):
        with pytest.raises(ValidationError):
            preserve_snapshot(
                store,
                v.tenant_id,
                v,
                b"data",
                origin_uri=uri,
                retrieved_at=datetime.now(timezone.utc),
            )
    with pytest.raises(PermissionError):
        preserve_snapshot(
            store,
            uuid4(),
            v,
            b"data",
            origin_uri="https://example.org/data",
            retrieved_at=datetime.now(timezone.utc),
        )
    with pytest.raises(ValueError):
        preserve_snapshot(
            store,
            v.tenant_id,
            v,
            b"",
            origin_uri="https://example.org/data",
            retrieved_at=datetime.now(timezone.utc),
        )
    store.preserve.assert_not_called()


def test_storage_failure_cannot_publish_manifest():
    v, store = version(), Mock()
    store.preserve.return_value = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="different"):
        preserve_snapshot(
            store,
            v.tenant_id,
            v,
            b"data",
            origin_uri="https://example.org/data",
            retrieved_at=datetime.now(timezone.utc),
        )
