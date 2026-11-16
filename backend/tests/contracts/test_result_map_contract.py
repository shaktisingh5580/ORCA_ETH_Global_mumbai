"""Purpose: protect P06 map and evidence provenance contract boundaries.
Behavior: reject missing map metadata and unsupported evidence references before publication.
Refs: main_implementation plan.md P06.1; Phase: P06.
"""

from uuid import uuid4

import pytest
from p06_evidence import insufficient_manifest
from pydantic import ValidationError

from orca.contracts.evidence import EvidenceClaim, MapDescriptor, validate_claim_support


def test_map_descriptor_bbox_crs_units_legend() -> None:
    descriptor = MapDescriptor(
        id=uuid4(),
        artifact_id=uuid4(),
        bbox=(60, 10, 70, 20),
        crs="EPSG:4326",
        unit="degC",
        legend="SST anomaly",
        source_manifest_digest="sha256:" + "a" * 64,
        status="available",
    )
    assert descriptor.crs == "EPSG:4326"
    with pytest.raises(ValidationError):
        MapDescriptor(
            id=uuid4(),
            artifact_id=uuid4(),
            bbox=(70, 10, 60, 20),
            crs="EPSG:4326",
            unit="degC",
            legend="",
            source_manifest_digest="sha256:" + "a" * 64,
            status="available",
        )


def test_evidence_claim_support_existing() -> None:
    manifest = insufficient_manifest()
    claim = EvidenceClaim(
        id=uuid4(),
        artifact_id=uuid4(),
        manifest_digest=manifest.manifest_digest,
        statement="The available evidence is insufficient.",
        support="insufficient_evidence",
    )
    validate_claim_support(claim, manifest)
    with pytest.raises(ValueError, match="different evidence"):
        validate_claim_support(
            claim.model_copy(update={"manifest_digest": "sha256:" + "0" * 64}), manifest
        )


def test_incomplete_result_blocks_publish() -> None:
    manifest = insufficient_manifest()
    claim = EvidenceClaim(
        id=uuid4(),
        artifact_id=uuid4(),
        manifest_digest=manifest.manifest_digest,
        statement="Sea surface temperature increased.",
        support="supported",
    )
    with pytest.raises(ValueError, match="complete"):
        validate_claim_support(claim, manifest)
