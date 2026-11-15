"""Purpose: define the frozen SST recipe request used by P04 policy validation.
Behavior: validates explicit method, time, baseline, measurement and unit declarations.
Refs: main_implementation plan.md P04.1; api design.md §§3.5,4.3; ADR-R02; Phase: P04.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SSTRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    method_id: str = Field(min_length=1, max_length=160)
    method_version: str = Field(min_length=1, max_length=80)
    method_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_qc_policy_id: str = Field(min_length=1, max_length=160)
    current_qc_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_qc_policy_id: str = Field(min_length=1, max_length=160)
    baseline_qc_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    as_of: datetime | None
    current_start: datetime
    current_end: datetime
    calendar: str = Field(min_length=1, max_length=80)
    baseline_years: tuple[int, ...]
    current_season: str = Field(min_length=1, max_length=80)
    baseline_season: str = Field(min_length=1, max_length=80)
    current_measurement: str = Field(min_length=1, max_length=160)
    baseline_measurement: str = Field(min_length=1, max_length=160)
    current_depth_m: float | None
    baseline_depth_m: float | None
    current_unit: str = Field(min_length=1, max_length=80)
    baseline_unit: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_recipe_shape(self) -> "SSTRecipe":
        for label, value in (
            ("current_start", self.current_start),
            ("current_end", self.current_end),
        ):
            if value.utcoffset() is None:
                raise ValueError(f"{label} must include an explicit timezone")
        if self.current_end <= self.current_start:
            raise ValueError("current interval must be non-empty and half-open")
        if len(set(self.baseline_years)) != len(self.baseline_years):
            raise ValueError("baseline years must be unique")
        if any(year < 1 or year > 9998 for year in self.baseline_years):
            raise ValueError("baseline years are outside the supported Gregorian range")
        return self
