"""Purpose: queue idempotent source checks and fence durable evidence publication.
Behavior: validate provider/version ownership, recover expired claims and conceal other tenants.
Refs: API 3.4; main plan P03.2/P03.4; Phase: P03.
"""

import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.application.projects import IdempotencyConflict
from orca.contracts.common import Operation
from orca.contracts.source_probes import ProbeRequest, SourceProbeEvidence, SourceProbeStatus


class SourceProbes:
    def __init__(self, session: Session, tenant: UUID):
        self.session, self.tenant = session, tenant

    def get(self, identifier: UUID) -> SourceProbeStatus | None:
        row = (
            self.session.execute(
                text("SELECT * FROM research.source_probes WHERE tenant_id=:tenant AND id=:id"),
                {"tenant": self.tenant, "id": identifier},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return SourceProbeStatus(
            operation=Operation(
                id=row["id"],
                kind="source_probe",
                status=row["status"],
                resource_ref=None,
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                links={"status": f"/v1/catalog/source-probes/{identifier}"},
            ),
            attempts=row["attempts"],
            error_code=row["error_code"],
            evidence=SourceProbeEvidence.model_validate(row["evidence"])
            if row["evidence"] is not None
            else None,
        )

    def create(self, body: ProbeRequest, key: str) -> SourceProbeStatus:
        if not 1 <= len(key) <= 128:
            raise ValueError("invalid idempotency key")
        provider = self.session.execute(
            text("""SELECT p.provider_id FROM research.catalog_versions v
          JOIN research.catalog_products p ON p.tenant_id=v.tenant_id AND p.id=v.product_id
          WHERE v.tenant_id=:tenant AND v.id=:version"""),
            {"tenant": self.tenant, "version": body.distribution_id},
        ).scalar_one_or_none()
        if provider is None or provider != body.provider_id:
            raise LookupError("distribution not found")
        payload = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        identifier = self.session.execute(
            text("""INSERT INTO research.source_probes
          (id,tenant_id,version_id,idempotency_key,request_hash,payload)
          VALUES(:id,:tenant,:version,:key,:hash,CAST(:payload AS jsonb))
          ON CONFLICT(tenant_id,idempotency_key) DO NOTHING RETURNING id"""),
            {
                "id": uuid4(),
                "tenant": self.tenant,
                "version": body.distribution_id,
                "key": key,
                "hash": digest,
                "payload": payload,
            },
        ).scalar_one_or_none()
        if identifier is None:
            row = self.session.execute(
                text(
                    "SELECT id,request_hash FROM research.source_probes WHERE tenant_id=:tenant AND idempotency_key=:key"
                ),
                {"tenant": self.tenant, "key": key},
            ).one()
            if row.request_hash != digest:
                raise IdempotencyConflict()
            identifier = row.id
        result = self.get(identifier)
        if result is None:
            raise RuntimeError("source probe disappeared")
        return result

    def claim(self) -> dict[str, Any] | None:
        row = (
            self.session.execute(
                text("""WITH candidate AS (
          SELECT id FROM research.source_probes WHERE tenant_id=:tenant AND
          (status='queued' OR (status='running' AND lease_until<clock_timestamp()))
          ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1)
          UPDATE research.source_probes p SET status='running', attempts=attempts+1,
          lease_token=:token,lease_until=clock_timestamp()+interval '60 seconds',updated_at=clock_timestamp()
          FROM candidate c WHERE p.id=c.id AND p.tenant_id=:tenant
          RETURNING p.id,p.payload,p.lease_token"""),
                {"tenant": self.tenant, "token": uuid4()},
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row else None

    def finish(self, identifier: UUID, token: UUID, evidence: SourceProbeEvidence | None) -> bool:
        result = self.session.execute(
            text("""UPDATE research.source_probes
          SET status=:status,evidence=CAST(:evidence AS jsonb),error_code=:error,
          lease_token=NULL,lease_until=NULL,updated_at=clock_timestamp()
          WHERE tenant_id=:tenant AND id=:id AND status='running'
          AND lease_token=:token AND lease_until>clock_timestamp() RETURNING id"""),
            {
                "tenant": self.tenant,
                "id": identifier,
                "token": token,
                "status": "succeeded" if evidence else "failed",
                "evidence": evidence.model_dump_json() if evidence else None,
                "error": None if evidence else "SOURCE_PROBE_REJECTED",
            },
        ).scalar_one_or_none()
        return result is not None
