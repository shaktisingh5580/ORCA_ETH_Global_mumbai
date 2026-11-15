"""Purpose: persist tenant-scoped, immutable scientific input and method lineage.
Behavior: insert a digest-verified manifest and FK-pinned snapshot references idempotently.
Refs: main_implementation plan.md P04.4; docs/04_database.md §§3,7; Phase: P04.
"""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.contracts.science_evidence import ScienceEvidenceManifest
from orca.science.manifests import verify_manifest


class ScientificEvidence:
    def __init__(self, session: Session, tenant_id: UUID, actor: str) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.actor = actor

    def persist(self, manifest: ScienceEvidenceManifest) -> UUID:
        verify_manifest(manifest)
        if manifest.tenant_id != self.tenant_id:
            raise PermissionError("science evidence tenant mismatch")
        if manifest.created_by != self.actor:
            raise PermissionError("science evidence actor mismatch")

        payload = manifest.model_dump(mode="json")
        inserted = self.session.execute(
            text("""INSERT INTO research.science_evidence_manifests
          (id,tenant_id,project_id,created_by,manifest,manifest_digest)
          VALUES(:id,:tenant,:project,:actor,CAST(:manifest AS jsonb),:digest)
          ON CONFLICT(tenant_id,project_id,manifest_digest) DO NOTHING RETURNING id"""),
            {
                "id": manifest.id,
                "tenant": self.tenant_id,
                "project": manifest.project_id,
                "actor": self.actor,
                "manifest": json.dumps(payload),
                "digest": manifest.manifest_digest,
            },
        ).scalar_one_or_none()
        if inserted is None:
            existing = (
                self.session.execute(
                    text("""SELECT id,manifest FROM research.science_evidence_manifests
              WHERE tenant_id=:tenant AND project_id=:project AND manifest_digest=:digest"""),
                    {
                        "tenant": self.tenant_id,
                        "project": manifest.project_id,
                        "digest": manifest.manifest_digest,
                    },
                )
                .mappings()
                .one_or_none()
            )
            if existing is None or existing["manifest"] != payload:
                raise ValueError("existing evidence digest does not identify the same manifest")
            return UUID(str(existing["id"]))

        self.session.execute(
            text("""INSERT INTO research.science_evidence_inputs
          (tenant_id,project_id,manifest_id,snapshot_id,input_role,raw_content_digest,byte_count)
          VALUES(:tenant,:project,:manifest,:snapshot,:role,:digest,:byte_count)"""),
            [
                {
                    "tenant": self.tenant_id,
                    "project": manifest.project_id,
                    "manifest": manifest.id,
                    "snapshot": item.snapshot_id,
                    "role": item.role,
                    "digest": item.raw_content_digest,
                    "byte_count": item.byte_count,
                }
                for item in manifest.inputs
            ],
        )
        return manifest.id

    def get(self, manifest_id: UUID, project_id: UUID) -> dict[str, Any] | None:
        row = (
            self.session.execute(
                text("""SELECT manifest,manifest_digest FROM research.science_evidence_manifests
          WHERE tenant_id=:tenant AND project_id=:project AND id=:id"""),
                {"tenant": self.tenant_id, "project": project_id, "id": manifest_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        manifest = ScienceEvidenceManifest.model_validate(row["manifest"])
        if manifest.manifest_digest != row["manifest_digest"]:
            raise ValueError("stored science evidence digest differs from manifest")
        verify_manifest(manifest)
        return manifest.model_dump(mode="json")
