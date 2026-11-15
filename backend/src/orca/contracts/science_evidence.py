"""Purpose: define immutable evidence lineage for one scientific calculation.
Behavior: bind snapshot bytes, method/QC recipe and result digests; reject unsupported claims.
Refs: main_implementation plan.md P04.4; docs/04_database.md §§3,7; Phase: P04.
"""

import hashlib
from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

import rfc8785
from pydantic import BaseModel, ConfigDict, Field, model_validator

_DIGEST = r"^sha256:[0-9a-f]{64}$"


class ScienceSnapshotInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: UUID
    role: Literal["current", "baseline"]
    year: int = Field(ge=1, le=9998)
    raw_content_digest: str = Field(pattern=_DIGEST)
    byte_count: int = Field(gt=0)
    object_version_id: str | None = Field(default=None, min_length=1, max_length=512)


class ScienceEvidenceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal["orca.science-evidence/1.0"] = "orca.science-evidence/1.0"
    id: UUID
    tenant_id: UUID
    project_id: UUID
    created_by: str = Field(min_length=1, max_length=255)
    created_at: datetime
    source_mode: Literal["live_real", "preserved_real", "synthetic_test"]
    method_id: str = Field(min_length=1, max_length=160)
    method_version: str = Field(min_length=1, max_length=80)
    method_code_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    method_approval_status: Literal["draft", "approved", "retired"]
    qc_policy_id: str = Field(min_length=1, max_length=160)
    qc_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    recipe_digest: str = Field(pattern=_DIGEST)
    result_digest: str = Field(pattern=_DIGEST)
    result_status: Literal["supported", "insufficient_evidence", "inconclusive", "invalid"]
    source_qualified: bool
    baseline_qualified: bool
    absolute_tolerance_celsius: float | None = Field(default=None, ge=0)
    uncertainty_celsius: float | None = Field(default=None, ge=0)
    inputs: tuple[ScienceSnapshotInput, ...] = Field(min_length=2, max_length=256)
    manifest_digest: str = Field(pattern=_DIGEST)

    @classmethod
    def build(cls, **values: Any) -> Self:
        draft = cls.model_construct(**values, manifest_digest="sha256:" + "0" * 64)
        payload = draft.model_dump(mode="json", exclude={"manifest_digest"})
        manifest_digest = "sha256:" + hashlib.sha256(rfc8785.dumps(payload)).hexdigest()
        return cls.model_validate({**payload, "manifest_digest": manifest_digest})

    @model_validator(mode="after")
    def validate_lineage(self) -> Self:
        if self.created_at.utcoffset() is None:
            raise ValueError("manifest creation time must include a timezone")
        current = [item for item in self.inputs if item.role == "current"]
        baseline = [item for item in self.inputs if item.role == "baseline"]
        if len(current) != 1 or not baseline:
            raise ValueError("lineage requires exactly one current and at least one baseline input")
        if len({item.snapshot_id for item in self.inputs}) != len(self.inputs):
            raise ValueError("lineage snapshot inputs must be unique")
        if len({item.year for item in baseline}) != len(baseline):
            raise ValueError("baseline input years must be unique")
        if any(item.year >= current[0].year for item in baseline):
            raise ValueError("baseline inputs must predate the current input")
        if self.result_status == "supported":
            if self.source_mode == "synthetic_test":
                raise ValueError("synthetic test data cannot support a scientific result")
            if (
                self.method_approval_status != "approved"
                or not self.source_qualified
                or not self.baseline_qualified
                or self.absolute_tolerance_celsius is None
            ):
                raise ValueError(
                    "supported result requires approved method, qualified inputs and tolerance"
                )
        expected = (
            "sha256:"
            + hashlib.sha256(
                rfc8785.dumps(self.model_dump(mode="json", exclude={"manifest_digest"}))
            ).hexdigest()
        )
        if self.manifest_digest != expected:
            raise ValueError("science evidence manifest digest does not match its content")
        return self
