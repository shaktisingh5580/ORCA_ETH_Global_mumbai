"""Purpose: verify P05 real-source fallback never presents unapproved science as a supported result.
Behavior: use test-only preserved bytes to exercise exact digest, trusted binding and replay invariants.
Refs: main_implementation plan.md P05.5; Phase: P05.
"""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from orca.application.planning import compile_typed_plan, plan_digest
from orca.contracts.catalog import Coverage, LicenseProfile, ProductVersion
from orca.contracts.research_plans import PlanRequest
from orca.contracts.snapshots import DatasetSnapshotManifest
from orca.infrastructure.jobs.postgres_queue import ClaimedJob
from orca.science.manifests import source_bytes_digest
from orca.workers.handlers.snapshot_evidence import PreservedSource, SnapshotInsufficiencyHandler
from orca.workers.runner import RunContext


class Reader:
    def __init__(self, sources: dict[object, PreservedSource]) -> None:
        self.sources = sources

    def read(self, identifier: object) -> PreservedSource:
        return self.sources[identifier]


def material() -> tuple[SnapshotInsufficiencyHandler, ClaimedJob, object, RunContext]:
    tenant, project = uuid4(), uuid4()
    sources: dict[object, PreservedSource] = {}
    trusted: dict[object, str] = {}
    for year in (2003, 2025):
        raw = f"pinned SST {year}".encode()
        version = ProductVersion(
            id=uuid4(),
            tenant_id=tenant,
            product_id=uuid4(),
            source_version=f"v{year}",
            metadata_digest="sha256:" + "a" * 64,
            native_variable="sst",
            standard_name="sea_surface_temperature",
            physical_definition="Test-only SST",
            native_units="K",
            calendar="gregorian",
            retrieved_at=datetime(2025, 2, 1, tzinfo=timezone.utc),
            license=LicenseProfile(
                license_ref="test", evidence_digest="sha256:" + "b" * 64, can_preserve=True
            ),
            coverage=Coverage(
                bbox=(68, 20, 69, 21),
                start=datetime(year, 1, 1, tzinfo=timezone.utc),
                end=datetime(year + 1, 1, 1, tzinfo=timezone.utc),
            ),
        )
        snapshot = DatasetSnapshotManifest(
            id=uuid4(),
            tenant_id=tenant,
            product_version=version,
            raw_content_digest=source_bytes_digest(raw),
            byte_count=len(raw),
            retrieved_at=datetime(2025, 2, 1, tzinfo=timezone.utc),
            origin_uri="https://example.org/pinned.csv",
        )
        sources[snapshot.id] = PreservedSource(snapshot, raw)
        trusted[version.id] = snapshot.origin_uri
    plan = compile_typed_plan(
        PlanRequest(
            strategy="typed",
            method_hint=None,
            data_constraints={},
            source_ids=list(sources),
            budget={"currency": "USD", "max_amount": "2.00"},
        ),
        revision=1,
    )
    context = RunContext(
        "researcher",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        plan_digest(plan, maximum_budget=Decimal("100")),
    )
    claimed = ClaimedJob(uuid4(), uuid4(), uuid4(), uuid4(), 1, {"node_count": 4})
    handler = SnapshotInsufficiencyHandler(tenant, project, Reader(sources), trusted)  # type: ignore[arg-type]
    return handler, claimed, plan, context


def test_preserved_inputs_yield_only_insufficient_evidence() -> None:
    handler, claimed, plan, context = material()
    result = handler.prepare(claimed, plan, context)  # type: ignore[arg-type]
    replay = handler.prepare(claimed, plan, context)  # type: ignore[arg-type]
    assert result.manifest.result_status == "insufficient_evidence"
    assert result.manifest.method_approval_status == "draft"
    assert not result.manifest.source_qualified and not result.manifest.baseline_qualified
    assert result.manifest.manifest_digest == replay.manifest.manifest_digest
    assert [stage.node_id for stage in result.stages] == [node.node_id for node in plan.nodes]  # type: ignore[attr-defined]


def test_unknown_source_binding_is_rejected() -> None:
    handler, claimed, plan, context = material()
    handler.trusted_sources = {uuid4(): "https://example.org/pinned.csv"}
    with pytest.raises(ValueError, match="approved"):
        handler.prepare(claimed, plan, context)  # type: ignore[arg-type]


def test_source_origin_must_match_trusted_binding() -> None:
    handler, claimed, plan, context = material()
    handler.trusted_sources = {
        identifier: "https://example.org/another.csv" for identifier in handler.trusted_sources
    }
    with pytest.raises(ValueError, match="approved"):
        handler.prepare(claimed, plan, context)  # type: ignore[arg-type]


def test_late_source_cannot_be_backdated_into_run() -> None:
    handler, claimed, plan, context = material()
    earlier = RunContext(
        context.actor, datetime(2024, 1, 1, tzinfo=timezone.utc), context.approved_digest
    )
    with pytest.raises(ValueError, match="available"):
        handler.prepare(claimed, plan, earlier)  # type: ignore[arg-type]
