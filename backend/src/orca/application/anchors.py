"""Purpose: request consented Arkiv anchors of completed reasoning and review evidence.
Behavior: hash an immutable private manifest; publish only identifiers and digests, not uploaded rows.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A04; Phase: A04 evidence publication increment.
"""

import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from orca.application.review import EvidenceReview
from orca.infrastructure.federation.signing import digest


class ResearchAnchors(EvidenceReview):
    def anchors(self, run: UUID) -> list[dict[str, Any]]:
        self.run_row(run)
        rows = self.session.execute(
            text("""SELECT id,run_id,manifest,manifest_digest,parent_key,
          state,envelope,receipt,error_code,created_at FROM research.evidence_anchors
          WHERE tenant_id=:t AND project_id=:p AND run_id=:run ORDER BY created_at"""),
            {**self.authorize(), "run": run},
        ).mappings()
        return [dict(row) for row in rows]

    def publish(self, run: UUID) -> dict[str, Any]:
        params = self.authorize("project:write")
        row = self.run_row(run, lock=True)
        if row["state"] != "completed":
            raise ValueError("Complete and approve the investigation before publication")
        reviews = self.reviews(run)
        manifest = {
            "schema_version": "orca.research-evidence/1.0",
            "publication_kind": "coordinator_attestation_not_a2a_execution",
            "run_id": str(run),
            "plan_digest": row["plan_digest"],
            "result_digest": row["result"]["result_digest"],
            "source_mode": "synthetic_demo"
            if any(a["source_mode"] == "synthetic_demo" for a in row["result"]["artifacts"])
            else "uploaded",
            "reviews": [
                {
                    "id": str(r["id"]),
                    "candidate_digest": r["candidate_digest"],
                    "decision_digest": digest(r["decision"]),
                    "findings_digest": digest(r["findings"]),
                    "parent_review_id": str(r["parent_review_id"])
                    if r["parent_review_id"]
                    else None,
                }
                for r in reviews
                if r["state"] == "completed"
            ],
        }
        checksum = digest(manifest)
        previous = self.anchors(run)
        for anchor in previous:
            if anchor["manifest_digest"] == checksum:
                return anchor
        if any(a["state"] in {"queued", "publishing", "reconciliation_required"} for a in previous):
            raise ValueError("Finish or reconcile the previous publication first")
        parent = next(
            (a["receipt"]["entity_key"] for a in reversed(previous) if a["state"] == "indexed"), ""
        )
        identifier = uuid4()
        self.session.execute(
            text("""INSERT INTO research.evidence_anchors
          (id,tenant_id,project_id,run_id,created_by,manifest,manifest_digest,parent_key)
          VALUES(:id,:t,:p,:run,:actor,CAST(:manifest AS jsonb),:digest,:parent)"""),
            {
                **params,
                "id": identifier,
                "run": run,
                "actor": self.principal.subject,
                "manifest": json.dumps(manifest),
                "digest": checksum,
                "parent": parent,
            },
        )
        self.event(
            run,
            "arkiv_publication_requested",
            {"anchor_id": str(identifier), "manifest_digest": checksum},
        )
        return next(a for a in self.anchors(run) if a["id"] == identifier)
