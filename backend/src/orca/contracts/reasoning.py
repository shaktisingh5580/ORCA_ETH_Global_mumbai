"""Purpose: define bounded evidence-based research plans and durable reasoning requests.
Behavior: constrain model-selected recipes and link every mapping to an authorized dataset.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A02; Phase: A02.
"""

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from orca.contracts.common import Contract
from orca.contracts.normalization import Mapping


class PlanDataset(Contract):
    dataset_id: UUID
    role: Literal["current", "reference", "representation"]
    mapping: Mapping


class ResearchPlan(Contract):
    outcome: Literal["supported", "needs_information", "unsupported"]
    recipe: Literal["normalize", "mean_difference"]
    summary: str = Field(min_length=1, max_length=3000)
    datasets: list[PlanDataset] = Field(max_length=4)
    missing_evidence: list[str] = Field(max_length=10)
    limitations: list[str] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def check_supported(self) -> "ResearchPlan":
        if len({d.dataset_id for d in self.datasets}) != len(self.datasets):
            raise ValueError("Dataset cannot be assigned twice")
        if self.outcome == "supported":
            if not self.datasets or self.missing_evidence:
                raise ValueError("Supported plan needs inputs and no unresolved evidence")
            roles = [d.role for d in self.datasets]
            if self.recipe == "mean_difference" and sorted(roles) != ["current", "reference"]:
                raise ValueError(
                    "Mean difference requires exactly one current and one reference input"
                )
        return self


class ReasoningRequest(Contract):
    question: str = Field(min_length=10, max_length=2000)
    dataset_ids: list[UUID] = Field(min_length=1, max_length=4)


class ReasoningApproval(Contract):
    approve: Literal[True]
    plan_digest: str
