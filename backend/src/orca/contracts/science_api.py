"""Purpose: define truthful P04 method, policy, template and recipe-validation API views.
Behavior: bound inputs and expose review status without treating metadata checks as scientific proof.
Refs: main_implementation plan.md P04; api design.md §3.5; Phase: P04.
"""

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from orca.contracts.common import Contract
from orca.contracts.science import SSTRecipe


class ScientificMethodView(Contract):
    method_id: str
    version: str
    kind: Literal["sst_anomaly"] = "sst_anomaly"
    code_digest: str
    approval_status: Literal["approved"] = "approved"
    required_baseline_years: int
    measurement_semantics: str
    qc_policy_id: str
    qc_policy_digest: str
    calendar: str
    limitations: tuple[str, ...] = (
        "Approval of a method does not establish source fitness or a supported finding",
    )


class ScientificMethodDetail(Contract):
    method_id: str
    kind: Literal["sst_anomaly"] = "sst_anomaly"
    approved_versions: tuple[ScientificMethodView, ...]


class ScientificMethodPage(Contract):
    items: tuple[ScientificMethodView, ...]
    page_size: int
    next_cursor: str | None = None


class RecipeTemplateView(Contract):
    template_id: str
    method_id: str
    method_version: str
    method_digest: str
    required_baseline_years: int
    qc_policy_id: str
    qc_policy_digest: str
    calendar: str
    measurement_semantics: str
    source_compatibility_verified: Literal[False] = False


class RecipeTemplatePage(Contract):
    items: tuple[RecipeTemplateView, ...]
    page_size: int
    next_cursor: str | None = None


class ScientificPolicySummary(Contract):
    method_id: str
    version: str
    qc_policy_id: str
    qc_policy_digest: str
    threshold_status: Literal["review_pending", "approved"]
    minimum_spatial_coverage: float | None = Field(default=None, gt=0, le=1)
    minimum_temporal_coverage: float | None = Field(default=None, gt=0, le=1)
    minimum_valid_samples: int | None = Field(default=None, ge=1)
    numerical_tolerance_celsius: float | None = Field(default=None, ge=0)
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def approved_thresholds_complete(self) -> "ScientificPolicySummary":
        if self.threshold_status == "approved" and any(
            value is None
            for value in (
                self.minimum_spatial_coverage,
                self.minimum_temporal_coverage,
                self.minimum_valid_samples,
                self.numerical_tolerance_celsius,
            )
        ):
            raise ValueError("approved policy requires all reviewed thresholds")
        return self


class QualityCodebook(Contract):
    source_id: str = Field(min_length=1)
    variable: str = Field(min_length=1)
    policy_id: str = Field(min_length=1)
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    flags: dict[str, str]
    review_status: Literal["approved"] = "approved"


class SelectedSources(Contract):
    current_version_id: UUID
    baseline_version_id: UUID


class RecipeValidationRequest(Contract):
    organization_id: UUID
    recipe: SSTRecipe
    selected_sources: SelectedSources


class RecipeValidationCheck(Contract):
    name: str
    status: Literal["pass", "fail", "unknown"]
    reason: str


class RecipeValidationReport(Contract):
    status: Literal["invalid", "incompatible", "requires_source_validation"]
    method_id: str
    method_version: str
    selected_sources: SelectedSources
    checks: tuple[RecipeValidationCheck, ...] = Field(min_length=1)
    scientific_finding: Literal[False] = False
