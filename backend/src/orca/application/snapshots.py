"""Purpose: store and retrieve immutable P03 snapshot provenance inside tenant transactions.
Behavior: verify pinned metadata against the stored version and conceal other tenants.
Refs: main plan P03.3/P03.5; API 3.4/4.2; Phase: P03.3.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.contracts.catalog import ProductVersion
from orca.contracts.snapshots import DatasetSnapshotManifest


class SnapshotRepository:
    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self.session, self.tenant_id = session, tenant_id

    def grant_project(
        self, snapshot_id: UUID, project_id: UUID, expires_at: datetime | None = None
    ) -> None:
        """Trusted application operation; callers must separately authorize project sharing."""
        self.session.execute(
            text("""INSERT INTO research.snapshot_project_grants
          (tenant_id,project_id,snapshot_id,expires_at) VALUES(:tenant,:project,:snapshot,:expires)
          ON CONFLICT(tenant_id,project_id,snapshot_id) DO UPDATE SET expires_at=EXCLUDED.expires_at"""),
            {
                "tenant": self.tenant_id,
                "project": project_id,
                "snapshot": snapshot_id,
                "expires": expires_at,
            },
        )

    def revoke_project(self, snapshot_id: UUID, project_id: UUID) -> None:
        self.session.execute(
            text("""DELETE FROM research.snapshot_project_grants
          WHERE tenant_id=:tenant AND project_id=:project AND snapshot_id=:snapshot"""),
            {"tenant": self.tenant_id, "project": project_id, "snapshot": snapshot_id},
        )

    def project_member(self, project_id: UUID, subject: str) -> bool:
        try:
            principal = UUID(subject)
        except ValueError:
            return False
        return (
            self.session.execute(
                text("""SELECT 1 FROM research.project_members m
          JOIN research.projects p ON p.tenant_id=m.tenant_id AND p.id=m.project_id
          WHERE m.tenant_id=:tenant AND m.project_id=:project AND m.principal_id=:principal
          AND m.status='active' AND p.state='active' LIMIT 1"""),
                {"tenant": self.tenant_id, "project": project_id, "principal": principal},
            ).scalar_one_or_none()
            is not None
        )

    def add(self, manifest: DatasetSnapshotManifest) -> None:
        if manifest.tenant_id != self.tenant_id:
            raise PermissionError("snapshot tenant mismatch")
        stored = self.session.execute(
            text("""SELECT manifest FROM research.catalog_versions
          WHERE tenant_id=:tenant AND id=:version"""),
            {"tenant": self.tenant_id, "version": manifest.product_version.id},
        ).scalar_one_or_none()
        if stored is None or ProductVersion.model_validate(stored) != manifest.product_version:
            raise ValueError("snapshot metadata does not match an accessible stored version")
        self.session.execute(
            text("""INSERT INTO research.dataset_snapshots
          (id,tenant_id,version_id,raw_content_digest,byte_count,manifest)
          VALUES(:id,:tenant,:version,:digest,:size,CAST(:manifest AS jsonb))"""),
            {
                "id": manifest.id,
                "tenant": self.tenant_id,
                "version": manifest.product_version.id,
                "digest": manifest.raw_content_digest,
                "size": manifest.byte_count,
                "manifest": manifest.model_dump_json(),
            },
        )

    def get(self, snapshot_id: UUID) -> DatasetSnapshotManifest | None:
        row = (
            self.session.execute(
                text("""SELECT * FROM research.dataset_snapshots
          WHERE tenant_id=:tenant AND id=:id"""),
                {"tenant": self.tenant_id, "id": snapshot_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return self._decode(dict(row))

    def list(
        self,
        *,
        after: UUID | None = None,
        limit: int = 50,
        product_id: UUID | None = None,
        digest: str | None = None,
        project_id: UUID | None = None,
    ) -> list[DatasetSnapshotManifest]:
        if not 1 <= limit <= 101:
            raise ValueError("page fetch limit must be between 1 and 101")
        rows = self.session.execute(
            text("""SELECT s.* FROM research.dataset_snapshots s
          JOIN research.catalog_versions v ON v.tenant_id=s.tenant_id AND v.id=s.version_id
          WHERE s.tenant_id=:tenant AND (CAST(:after AS uuid) IS NULL OR s.id>CAST(:after AS uuid))
          AND (CAST(:product AS uuid) IS NULL OR v.product_id=CAST(:product AS uuid))
          AND (CAST(:digest AS text) IS NULL OR s.raw_content_digest=:digest)
          AND (CAST(:project AS uuid) IS NULL OR EXISTS(
            SELECT 1 FROM research.snapshot_project_grants g WHERE g.tenant_id=s.tenant_id
            AND g.snapshot_id=s.id AND g.project_id=CAST(:project AS uuid)
            AND (g.expires_at IS NULL OR g.expires_at>clock_timestamp())))
          ORDER BY s.id LIMIT :limit"""),
            {
                "tenant": self.tenant_id,
                "after": after,
                "limit": limit,
                "product": product_id,
                "digest": digest,
                "project": project_id,
            },
        )
        return [self._decode(dict(row)) for row in rows.mappings()]

    @staticmethod
    def _decode(row: Mapping[str, Any]) -> DatasetSnapshotManifest:
        manifest = DatasetSnapshotManifest.model_validate(row["manifest"])
        if (
            manifest.id != row["id"]
            or manifest.tenant_id != row["tenant_id"]
            or manifest.product_version.id != row["version_id"]
            or manifest.raw_content_digest != row["raw_content_digest"]
            or manifest.byte_count != row["byte_count"]
        ):
            raise ValueError("snapshot manifest identity mismatch")
        return manifest
