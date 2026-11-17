"""Purpose: record and select P07 archived observation bytes for historical evaluation.
Behavior: stamp verified acquisition facts once and fail closed on absent, future or corrupt evidence.
Refs: main_implementation plan.md P07.3; api design.md §§3.8, 4.5; Phase: P07.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.contracts.snapshot_requests import SnapshotRequest
from orca.contracts.snapshots import DatasetSnapshotManifest
from orca.science.temporal import HistoricalSnapshot, select_historical_snapshot


class HistoricalByteStore(Protocol):
    def read(self, tenant: UUID, digest: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class HistoricalReplaySelection:
    status: Literal["selected", "inconclusive", "blocked"]
    snapshot_id: UUID | None
    content_digest: str | None
    reason: str


class HistoricalReplay:
    def __init__(self, session: Session, tenant: UUID, store: HistoricalByteStore | None = None):
        self.session, self.tenant, self.store = session, tenant, store

    def record_observation(
        self, snapshot: DatasetSnapshotManifest, request: SnapshotRequest
    ) -> bool:
        """Called only after trusted acquisition and exact-byte read-back, in its transaction."""
        version = snapshot.product_version
        if snapshot.tenant_id != self.tenant or request.product_version_id != version.id:
            raise ValueError("archive acquisition identity mismatch")
        if snapshot.acquisition_request_digest is None:
            return False
        if version.published_at is None or version.available_at is None:
            return False
        if version.available_at > snapshot.retrieved_at:
            return False
        if request.time_window.end > snapshot.retrieved_at:
            return False
        kind = self.session.execute(
            text("""SELECT kind,status FROM research.catalog_products
              WHERE tenant_id=:tenant AND id=:source"""),
            {"tenant": self.tenant, "source": version.product_id},
        ).one_or_none()
        if kind is None or kind.kind != "observation" or kind.status != "validated":
            return False
        self.session.execute(
            text("""INSERT INTO research.historical_archive_entries
              (id,tenant_id,snapshot_id,source_id,content_digest,request_digest,byte_count,
               object_version_id,retention_kind,published_at,available_at,retrieved_at,valid_at)
              VALUES(:id,:tenant,:snapshot,:source,:digest,:request,:size,:version,
                     'content_addressed_readback',:published,:available,:retrieved,:valid)"""),
            {
                "id": uuid4(),
                "tenant": self.tenant,
                "snapshot": snapshot.id,
                "source": version.product_id,
                "digest": snapshot.raw_content_digest,
                "request": snapshot.acquisition_request_digest,
                "size": snapshot.byte_count,
                "version": snapshot.raw_content_digest,
                "published": version.published_at,
                "available": version.available_at,
                "retrieved": snapshot.retrieved_at,
                "valid": request.time_window.end,
            },
        )
        return True

    def select(
        self, *, project_id: UUID, source_id: UUID, as_of: datetime
    ) -> HistoricalReplaySelection:
        """Return a byte-verified archived original or an explicit non-evaluable result."""
        if self.store is None:
            raise RuntimeError("historical replay requires an exact-byte store")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("historical cutoff requires a timezone")
        cutoff = as_of.astimezone(timezone.utc)
        rows = (
            self.session.execute(
                text("""SELECT h.*,s.manifest AS snapshot_manifest
                  FROM research.historical_archive_entries h
                  JOIN research.dataset_snapshots s
                    ON s.tenant_id=h.tenant_id AND s.id=h.snapshot_id
                  WHERE h.tenant_id=:tenant AND h.source_id=:source
                    AND h.published_at<=:cutoff AND h.available_at<=:cutoff
                    AND h.retrieved_at<=:cutoff AND h.archived_at<=:cutoff
                    AND EXISTS(SELECT 1 FROM research.snapshot_project_grants g
                      WHERE g.tenant_id=h.tenant_id AND g.snapshot_id=h.snapshot_id
                        AND g.project_id=:project
                        AND (g.expires_at IS NULL OR g.expires_at>clock_timestamp()))
                  ORDER BY h.published_at DESC,h.id DESC LIMIT 1000"""),
                {
                    "tenant": self.tenant,
                    "source": source_id,
                    "project": project_id,
                    "cutoff": cutoff,
                },
            )
            .mappings()
            .all()
        )
        candidates = tuple(
            HistoricalSnapshot(
                id=row["snapshot_id"],
                source_id=row["source_id"],
                object_version_id=row["object_version_id"],
                published_at=row["published_at"],
                retrieved_at=row["retrieved_at"],
                valid_at=row["valid_at"],
                issued_at=row["issued_at"],
                archived_original=row["archived_original"],
                supersedes_id=row["supersedes_id"],
                available_at=row["available_at"],
                archived_at=row["archived_at"],
            )
            for row in rows
        )
        selected = select_historical_snapshot(candidates, source_id=source_id, as_of=as_of)
        if selected is None:
            return HistoricalReplaySelection(
                "inconclusive", None, None, "NO_ARCHIVED_ORIGINAL_AVAILABLE_AT_CUTOFF"
            )
        row = next(row for row in rows if row["snapshot_id"] == selected.id)
        manifest = DatasetSnapshotManifest.model_validate(row["snapshot_manifest"])
        if (
            manifest.id != selected.id
            or manifest.tenant_id != self.tenant
            or manifest.product_version.product_id != source_id
            or manifest.raw_content_digest != row["content_digest"]
            or manifest.byte_count != row["byte_count"]
            or manifest.acquisition_request_digest != row["request_digest"]
            or manifest.retrieved_at != row["retrieved_at"]
            or manifest.product_version.published_at != row["published_at"]
            or manifest.product_version.available_at != row["available_at"]
            or selected.object_version_id != row["content_digest"]
        ):
            return HistoricalReplaySelection("blocked", None, None, "ARCHIVE_LINEAGE_MISMATCH")
        try:
            raw = self.store.read(self.tenant, row["content_digest"])
        except (OSError, ValueError):
            return HistoricalReplaySelection("blocked", None, None, "ARCHIVED_BYTES_UNAVAILABLE")
        if len(raw) != row["byte_count"] or (
            "sha256:" + hashlib.sha256(raw).hexdigest() != row["content_digest"]
        ):
            return HistoricalReplaySelection("blocked", None, None, "ARCHIVED_BYTES_MISMATCH")
        return HistoricalReplaySelection(
            "selected", selected.id, row["content_digest"], "ARCHIVED_ORIGINAL_VERIFIED"
        )
