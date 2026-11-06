"""Purpose: enqueue idempotent acquisitions and fence worker completion after crashes.
Behavior: verify stored rights, lease requests atomically and reject stale completions.
Refs: API 3.4; main plan P03.3/P03.5; Phase: P03.5.
"""

import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.application.projects import IdempotencyConflict
from orca.contracts.catalog import ProductVersion
from orca.contracts.common import Operation
from orca.contracts.snapshot_requests import SnapshotRequest, SnapshotRequestStatus, request_digest


class SnapshotRequests:
    def __init__(self, session: Session, tenant: UUID):
        self.session, self.tenant = session, tenant

    def get(self, request_id: UUID) -> SnapshotRequestStatus | None:
        row = (
            self.session.execute(
                text("SELECT * FROM research.snapshot_requests WHERE tenant_id=:tenant AND id=:id"),
                {"tenant": self.tenant, "id": request_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return SnapshotRequestStatus(
            operation=Operation(
                id=row["id"],
                kind="snapshot",
                status=row["status"],
                resource_ref=None,
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                links={"status": f"/v1/catalog/snapshot-requests/{row['id']}"},
            ),
            snapshot_id=row["snapshot_id"],
            error_code=row["error_code"],
            attempts=row["attempts"],
        )

    def create(self, body: SnapshotRequest, key: str) -> SnapshotRequestStatus:
        if not 1 <= len(key) <= 128:
            raise ValueError("invalid idempotency key")
        payload = body.model_dump(mode="json")
        digest = request_digest(body).removeprefix("sha256:")
        stored = self.session.execute(
            text(
                "SELECT manifest FROM research.catalog_versions WHERE tenant_id=:tenant AND id=:id"
            ),
            {"tenant": self.tenant, "id": body.product_version_id},
        ).scalar_one_or_none()
        if stored is None:
            raise LookupError("version not found")
        version = ProductVersion.model_validate(stored)
        if not version.can_preserve_for(self.tenant):
            raise PermissionError("preservation rights unavailable")
        if body.variables != [version.native_variable]:
            raise ValueError("only the stored scientific variable is approved for this request")
        if body.depth is not None and body.depth != version.depth_m:
            raise ValueError("requested depth does not match stored version")
        inserted = self.session.execute(
            text("""INSERT INTO research.snapshot_requests
            (id,tenant_id,version_id,idempotency_key,request_hash,payload)
            VALUES(:id,:tenant,:version,:key,:hash,CAST(:payload AS jsonb))
            ON CONFLICT(tenant_id,idempotency_key) DO NOTHING RETURNING id"""),
            {
                "id": uuid4(),
                "tenant": self.tenant,
                "version": version.id,
                "key": key,
                "hash": digest,
                "payload": json.dumps(payload),
            },
        ).scalar_one_or_none()
        if inserted is None:
            row = self.session.execute(
                text(
                    "SELECT id,request_hash FROM research.snapshot_requests WHERE tenant_id=:tenant AND idempotency_key=:key"
                ),
                {"tenant": self.tenant, "key": key},
            ).one()
            if row.request_hash != digest:
                raise IdempotencyConflict()
            inserted = row.id
        result = self.get(inserted)
        if result is None:
            raise RuntimeError("request disappeared")
        return result

    def claim(self, lease_seconds: int = 60) -> dict[str, Any] | None:
        if not 1 <= lease_seconds <= 300:
            raise ValueError("invalid lease duration")
        row = (
            self.session.execute(
                text("""WITH candidate AS (
          SELECT id FROM research.snapshot_requests WHERE tenant_id=:tenant
          AND (status='queued' OR (status='running' AND lease_until<clock_timestamp()))
          ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1)
          UPDATE research.snapshot_requests r SET status='running', attempts=attempts+1,
          lease_token=:token, lease_until=clock_timestamp()+make_interval(secs=>:seconds),
          updated_at=clock_timestamp() FROM candidate c WHERE r.id=c.id AND r.tenant_id=:tenant
          RETURNING r.id,r.payload,r.lease_token,r.attempts"""),
                {"tenant": self.tenant, "token": uuid4(), "seconds": lease_seconds},
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row else None

    def fail(self, request_id: UUID, token: UUID, error_code: str) -> bool:
        if (
            not error_code.isupper()
            or not error_code.replace("_", "").isalnum()
            or len(error_code) > 80
        ):
            raise ValueError("error must be a sanitized uppercase code")
        result = self.session.execute(
            text("""UPDATE research.snapshot_requests
          SET status='failed',error_code=:error,lease_token=NULL,lease_until=NULL,updated_at=clock_timestamp()
          WHERE tenant_id=:tenant AND id=:id AND status='running' AND lease_token=:token
          AND lease_until>clock_timestamp() RETURNING id"""),
            {"tenant": self.tenant, "id": request_id, "token": token, "error": error_code},
        ).scalar_one_or_none()
        return result is not None

    def complete(self, request_id: UUID, token: UUID, snapshot_id: UUID) -> bool:
        result = self.session.execute(
            text("""UPDATE research.snapshot_requests r
          SET status='succeeded',snapshot_id=:snapshot,lease_token=NULL,lease_until=NULL,
          updated_at=clock_timestamp() WHERE r.tenant_id=:tenant AND r.id=:id
          AND r.status='running' AND r.lease_token=:token AND r.lease_until>clock_timestamp()
          AND EXISTS(SELECT 1 FROM research.dataset_snapshots s WHERE s.tenant_id=r.tenant_id
            AND s.id=:snapshot AND s.version_id=r.version_id
            AND s.manifest->>'acquisition_request_digest'='sha256:' || r.request_hash)
          RETURNING r.id"""),
            {"tenant": self.tenant, "id": request_id, "token": token, "snapshot": snapshot_id},
        ).scalar_one_or_none()
        return result is not None
