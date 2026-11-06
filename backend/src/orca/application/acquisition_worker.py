"""Purpose: execute one durable snapshot acquisition without holding a network transaction.
Behavior: commit a lease, fetch through a configured port, then publish with fencing atomically.
Refs: main implementation plan P03.3/P03.5; API 3.4; Phase: P03.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.application.ingestion import SnapshotStore, preserve_snapshot
from orca.application.snapshot_requests import SnapshotRequests
from orca.application.snapshots import SnapshotRepository
from orca.contracts.catalog import ProductVersion
from orca.contracts.snapshot_requests import SnapshotRequest, request_digest


@dataclass(frozen=True)
class AcquisitionResult:
    content: bytes
    origin_uri: str
    request_digest: str


class AcquisitionSource(Protocol):
    def fetch(self, request: SnapshotRequest, version: ProductVersion) -> AcquisitionResult:
        """Return bounded bytes only after verifying requested coordinates and source metadata."""
        ...


class AcquisitionFailed(Exception):
    """Expected adapter failure; details are never exposed to API clients."""


class LeaseLost(Exception):
    """Force rollback of manifest publication if another worker owns the request."""


class AcquisitionWorker:
    def __init__(
        self,
        tenant: UUID,
        transaction: Callable[[], AbstractContextManager[Session]],
        source: AcquisitionSource,
        store: SnapshotStore,
    ):
        self.tenant, self.transaction = tenant, transaction
        self.source, self.store = source, store

    def run_once(self) -> Literal["idle", "succeeded", "failed", "lease_lost"]:
        with self.transaction() as session:
            claim = SnapshotRequests(session, self.tenant).claim(lease_seconds=300)
            if claim is None:
                return "idle"
            body = SnapshotRequest.model_validate(claim["payload"])
            stored: Any = session.execute(
                text(
                    "SELECT manifest FROM research.catalog_versions WHERE tenant_id=:tenant AND id=:id"
                ),
                {"tenant": self.tenant, "id": body.product_version_id},
            ).scalar_one()
            version = ProductVersion.model_validate(stored)
        # Lease is durable before any external I/O. Crashes leave recoverable running work.
        try:
            if not version.can_preserve_for(self.tenant):
                raise AcquisitionFailed()
            result = self.source.fetch(body, version)
            if result.request_digest != request_digest(body):
                raise AcquisitionFailed()
            manifest = preserve_snapshot(
                self.store,
                self.tenant,
                version,
                result.content,
                origin_uri=result.origin_uri,
                retrieved_at=datetime.now(timezone.utc),
                acquisition_request_digest=result.request_digest,
            )
        except (AcquisitionFailed, ValueError, PermissionError):
            with self.transaction() as session:
                failed = SnapshotRequests(session, self.tenant).fail(
                    claim["id"], claim["lease_token"], "ACQUISITION_REJECTED"
                )
            return "failed" if failed else "lease_lost"
        # Unexpected infrastructure errors propagate; the expired lease remains recoverable.
        # Immutable content may remain after a crash, but no manifest is published partially.
        try:
            with self.transaction() as session:
                SnapshotRepository(session, self.tenant).add(manifest)
                if not SnapshotRequests(session, self.tenant).complete(
                    claim["id"], claim["lease_token"], manifest.id
                ):
                    raise LeaseLost()
        except LeaseLost:
            return "lease_lost"
        return "succeeded"
