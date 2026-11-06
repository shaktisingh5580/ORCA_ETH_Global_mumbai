"""Purpose: queue evidence reviews and approve corrective recomputation as new candidates.
Behavior: preserve originals, require current project authority and bind corrections to reviewed findings.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A03; Phase: A03.
"""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from orca.application.reasoning import ResearchReasoning
from orca.contracts.normalization import Mapping
from orca.contracts.review import CandidateResult
from orca.domain.normalization import digest, normalize
from orca.domain.tabular_analysis import summarize


class EvidenceReview(ResearchReasoning):
    def reference(self, run: UUID) -> dict[str, Any]:
        row = self.run_row(run)
        if row["state"] != "completed":
            raise ValueError("Review requires a completed approved computation")
        # Recompute independently from original immutable input and approved mapping.
        artifacts = []
        for item in row["plan"]["datasets"]:
            source = self.row(UUID(item["dataset_id"]))
            mapping = Mapping.model_validate(item["mapping"])
            normalized = normalize(source["content"], source["format"], mapping)
            normalized.update(
                source_mode=source["source_mode"],
                source_digest=source["content_digest"],
                mapping_digest=digest(mapping.model_dump()),
            )
            artifacts.append(
                {
                    "dataset_id": item["dataset_id"],
                    "role": item["role"],
                    "artifact_digest": digest(normalized),
                    "summary": summarize(normalized),
                    "source_mode": source["source_mode"],
                }
            )
        return {"plan_digest": row["plan_digest"], "artifacts": artifacts}

    def submit_review(
        self, run: UUID, candidate: CandidateResult, parent: UUID | None = None
    ) -> dict[str, Any]:
        params = self.authorize("project:write")
        reference = self.reference(run)
        if (
            any(a["source_mode"] == "synthetic_demo" for a in reference["artifacts"])
            and candidate.source_mode != "synthetic_demo"
        ):
            raise ValueError("Synthetic evidence requires an explicit synthetic candidate label")
        params.update(
            id=uuid4(),
            run=run,
            actor=self.principal.subject,
            candidate=candidate.model_dump_json(),
            digest=digest(candidate.model_dump(mode="json")),
            parent=parent,
        )
        self.session.execute(
            text("""INSERT INTO research.evidence_reviews
          (id,tenant_id,project_id,run_id,created_by,candidate,candidate_digest,parent_review_id)
          VALUES(:id,:t,:p,:run,:actor,CAST(:candidate AS jsonb),:digest,:parent)
          ON CONFLICT(tenant_id,project_id,run_id,candidate_digest) DO NOTHING"""),
            params,
        )
        row = (
            self.session.execute(
                text("""SELECT * FROM research.evidence_reviews
          WHERE tenant_id=:t AND project_id=:p AND run_id=:run AND candidate_digest=:digest"""),
                params,
            )
            .mappings()
            .one()
        )
        return self.review_view(row)

    @staticmethod
    def review_view(row: Any) -> dict[str, Any]:
        return {
            k: row[k]
            for k in (
                "id",
                "run_id",
                "candidate",
                "candidate_digest",
                "parent_review_id",
                "state",
                "findings",
                "decision",
                "error_code",
                "created_at",
            )
        }

    def review_row(self, identifier: UUID) -> Any:
        row = (
            self.session.execute(
                text("""SELECT * FROM research.evidence_reviews WHERE tenant_id=:t
          AND project_id=:p AND id=:id"""),
                {**self.authorize(), "id": identifier},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("Review not found")
        return row

    def reviews(self, run: UUID) -> list[dict[str, Any]]:
        self.run_row(run)
        rows = self.session.execute(
            text("""SELECT * FROM research.evidence_reviews WHERE tenant_id=:t
          AND project_id=:p AND run_id=:run ORDER BY created_at"""),
            {**self.authorize(), "run": run},
        ).mappings()
        return [self.review_view(r) for r in rows]

    def correct(self, identifier: UUID, expected_digest: str) -> dict[str, Any]:
        self.authorize("project:write")
        reviewed = self.review_row(identifier)
        if reviewed["candidate_digest"] != expected_digest or reviewed["state"] != "completed":
            raise ValueError("Review changed or incomplete")
        if (
            reviewed["decision"]["coordinator"]["action"] != "recompute_approved_plan"
            or not reviewed["findings"]
        ):
            raise ValueError("Reviewer has not proposed an executable correction")
        reference = self.reference(reviewed["run_id"])
        modes = {a["source_mode"] for a in reference["artifacts"]}
        candidate = CandidateResult(
            label="Approved correction of " + str(identifier),
            source_mode="synthetic_demo" if "synthetic_demo" in modes else "computed",
            plan_digest=reference["plan_digest"],
            means_celsius={
                a["dataset_id"]: a["summary"]["mean_celsius"] for a in reference["artifacts"]
            },
            evidence_digests={
                a["dataset_id"]: a["artifact_digest"] for a in reference["artifacts"]
            },
        )
        result = self.submit_review(reviewed["run_id"], candidate, identifier)
        self.event(
            reviewed["run_id"],
            "correction_approved",
            {
                "parent_review_id": str(identifier),
                "new_review_id": str(result["id"]),
                "candidate_digest": result["candidate_digest"],
            },
        )
        return result
