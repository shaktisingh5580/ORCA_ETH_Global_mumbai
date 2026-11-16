"""Purpose: turn preserved, pinned P03 source bytes into a truthful P05 insufficiency artifact.
Behavior: verify real source bindings and exact bytes; never claim approved science before a method is registered.
Refs: main_implementation plan.md P05.5/P06.1; api design.md §6.2; Phase: P05.
"""

import hashlib
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import rfc8785
from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.application.snapshots import SnapshotRepository
from orca.contracts.research_plans import TypedPlan
from orca.contracts.science_evidence import ScienceEvidenceManifest, ScienceSnapshotInput
from orca.contracts.snapshots import DatasetSnapshotManifest
from orca.infrastructure.jobs.postgres_queue import ClaimedJob
from orca.infrastructure.storage.snapshot_s3 import SnapshotS3
from orca.science.manifests import source_bytes_digest
from orca.workers.runner import RunContext, ScientificExecution, StageOutput


@dataclass(frozen=True, slots=True)
class PreservedSource:
    manifest: DatasetSnapshotManifest
    content: bytes


class ProjectSnapshotReader:
    """Read a project-granted snapshot and its immutable content-addressed bytes."""

    def __init__(
        self,
        tenant: UUID,
        project: UUID,
        transaction: Callable[[], AbstractContextManager[Session]],
        store: SnapshotS3,
    ) -> None:
        self.tenant, self.project, self.transaction, self.store = (
            tenant,
            project,
            transaction,
            store,
        )

    def read(self, snapshot_id: UUID) -> PreservedSource:
        with self.transaction() as session:
            allowed = session.execute(
                text("""SELECT 1 FROM research.snapshot_project_grants WHERE tenant_id=:tenant
                  AND project_id=:project AND snapshot_id=:snapshot
                  AND (expires_at IS NULL OR expires_at>clock_timestamp())"""),
                {"tenant": self.tenant, "project": self.project, "snapshot": snapshot_id},
            ).scalar_one_or_none()
            if allowed is None:
                raise PermissionError("source snapshot is not granted to this project")
            manifest = SnapshotRepository(session, self.tenant).get(snapshot_id)
            if manifest is None:
                raise LookupError("source snapshot is missing")
        content = self.store.read(self.tenant, manifest.raw_content_digest)
        if len(content) != manifest.byte_count:
            raise ValueError("source byte count differs from immutable snapshot manifest")
        return PreservedSource(manifest, content)


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(rfc8785.dumps(cast(Any, value))).hexdigest()


class SnapshotInsufficiencyHandler:
    """Publish a real-source trace that explicitly says no approved method was run."""

    def __init__(
        self,
        tenant: UUID,
        project: UUID,
        reader: ProjectSnapshotReader,
        trusted_sources: Mapping[UUID, str],
    ) -> None:
        self.tenant, self.project, self.reader = tenant, project, reader
        self.trusted_sources = trusted_sources

    def prepare(
        self, claimed: ClaimedJob, plan: TypedPlan, context: RunContext
    ) -> ScientificExecution:
        if not 2 <= len(plan.source_ids) <= 8 or not self.trusted_sources:
            raise ValueError("at least two approved preserved sources are required")
        if [(node.node_id, node.operation) for node in plan.nodes] != [
            ("source-metadata", "source_metadata"),
            ("compatibility", "compatibility_check"),
            ("scientific-method", "scientific_method"),
            ("evidence-manifest", "evidence_manifest"),
        ]:
            raise ValueError("unsupported research plan shape")
        sources = tuple(self.reader.read(identifier) for identifier in plan.source_ids)
        years: dict[int, PreservedSource] = {}
        definitions: set[tuple[str, str]] = set()
        for source in sources:
            snapshot_manifest = source.manifest
            version = snapshot_manifest.product_version
            coverage = version.coverage
            if (
                snapshot_manifest.tenant_id != self.tenant
                or snapshot_manifest.id not in plan.source_ids
                or snapshot_manifest.origin_uri != self.trusted_sources.get(version.id)
                or snapshot_manifest.retrieved_at > context.created_at
                or version.retrieved_at > context.created_at
                or coverage is None
            ):
                raise ValueError("source is not an approved, available preserved snapshot")
            year = coverage.start.year
            if (coverage.end - timedelta(microseconds=1)).year != year or year in years:
                raise ValueError("each source must cover one distinct calendar year")
            if (
                len(source.content) != snapshot_manifest.byte_count
                or source_bytes_digest(source.content) != snapshot_manifest.raw_content_digest
            ):
                raise ValueError("preserved source bytes differ from the pinned manifest")
            years[year] = source
            definitions.add((version.standard_name, version.calendar))
        if len(definitions) != 1:
            raise ValueError("source variables and calendars are incompatible")
        ordered = tuple(sorted(years.items(), reverse=True))
        current_year = ordered[0][0]
        west = max(item.manifest.product_version.coverage.bbox[0] for _, item in ordered)  # type: ignore[union-attr]
        south = max(item.manifest.product_version.coverage.bbox[1] for _, item in ordered)  # type: ignore[union-attr]
        east = min(item.manifest.product_version.coverage.bbox[2] for _, item in ordered)  # type: ignore[union-attr]
        north = min(item.manifest.product_version.coverage.bbox[3] for _, item in ordered)  # type: ignore[union-attr]
        if west >= east or south >= north:
            raise ValueError("source footprints do not overlap")
        inputs = tuple(
            ScienceSnapshotInput(
                snapshot_id=item.manifest.id,
                role="current" if year == current_year else "baseline",
                year=year,
                raw_content_digest=item.manifest.raw_content_digest,
                byte_count=item.manifest.byte_count,
                object_version_id=item.manifest.raw_content_digest,
            )
            for year, item in ordered
        )
        reason = {
            "reason": "no_approved_method_executed",
            "plan": context.approved_digest,
            "snapshots": [str(item.snapshot_id) for item in inputs],
        }
        evidence_manifest = ScienceEvidenceManifest.build(
            id=uuid5(NAMESPACE_URL, f"orca:run:{claimed.run_id}:insufficient-evidence"),
            tenant_id=self.tenant,
            project_id=self.project,
            created_by=context.actor,
            created_at=context.created_at,
            source_mode="preserved_real",
            method_id="method_unavailable",
            method_version="0",
            method_code_digest="0" * 64,
            method_approval_status="draft",
            qc_policy_id="not_evaluated",
            qc_policy_digest="0" * 64,
            recipe_digest=context.approved_digest,
            result_digest=_digest(reason),
            result_status="insufficient_evidence",
            source_qualified=False,
            baseline_qualified=False,
            inputs=inputs,
        )
        source_digest = _digest([item.manifest.model_dump(mode="json") for item in sources])
        compatible_digest = _digest({"years": sorted(years), "bbox": [west, south, east, north]})
        unavailable_digest = _digest(reason)
        return ScientificExecution(
            stages=(
                StageOutput("source-metadata", context.approved_digest, source_digest),
                StageOutput("compatibility", source_digest, compatible_digest),
                StageOutput("scientific-method", compatible_digest, unavailable_digest),
                StageOutput(
                    "evidence-manifest", unavailable_digest, evidence_manifest.manifest_digest
                ),
            ),
            manifest=evidence_manifest,
        )
