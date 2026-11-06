"""Purpose: preserve authorized P03 source bytes and construct immutable provenance.
Behavior: validate before writing, verify storage digest and never synthesize missing data.
Refs: main plan P03.3; research-only guide 5.2-5.3; Phase: P03.3.
"""

import hashlib
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from orca.contracts.catalog import ProductVersion
from orca.contracts.snapshots import DatasetSnapshotManifest


class SnapshotStore(Protocol):
    def preserve(self, tenant: UUID, content: bytes, *, permitted: bool) -> str: ...


def preserve_snapshot(
    store: SnapshotStore,
    tenant_id: UUID,
    version: ProductVersion,
    content: bytes,
    *,
    origin_uri: str,
    retrieved_at: datetime,
    max_bytes: int = 16 * 1024 * 1024,
) -> DatasetSnapshotManifest:
    if not version.can_preserve_for(tenant_id):
        raise PermissionError("source preservation is not authorized")
    if not content or len(content) > max_bytes:
        raise ValueError("empty or oversized source bytes")
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    manifest = DatasetSnapshotManifest(
        id=uuid4(),
        tenant_id=tenant_id,
        product_version=version,
        raw_content_digest=digest,
        byte_count=len(content),
        retrieved_at=retrieved_at,
        origin_uri=origin_uri,
    )
    stored_digest = store.preserve(tenant_id, content, permitted=True)
    if stored_digest != digest:
        raise ValueError("storage returned a different content digest")
    return manifest
