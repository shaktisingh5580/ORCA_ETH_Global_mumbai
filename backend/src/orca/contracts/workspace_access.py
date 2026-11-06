"""Purpose: define explicit project grants and bounded organization policy contracts.
Behavior: reject unknown roles, empty patches and changes beyond platform quota limits.
Refs: API 3.2/3.3; main plan P02; Phase: P02 completion repair.
"""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from orca.contracts.common import Contract

ProjectRole = Literal["admin", "editor", "reviewer", "viewer"]


class AddProjectMember(Contract):
    # principal_id identifies an existing organization Membership.id, not a token subject.
    principal_id: UUID
    role: ProjectRole


class PatchProjectMember(Contract):
    role: ProjectRole | None = None
    status: Literal["active", "revoked"] | None = None

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if self.role is None and self.status is None:
            raise ValueError("project membership changes required")
        return self


class ProjectMemberDetail(Contract):
    id: UUID
    project_id: UUID
    principal_id: UUID
    role: ProjectRole
    status: Literal["active", "revoked"]
    version: int
    etag: str


class PatchOrgPolicy(Contract):
    retention_days: int | None = Field(default=None, ge=30, le=3650)
    max_upload_bytes: int | None = Field(default=None, ge=1, le=104857600)
    max_projects: int | None = Field(default=None, ge=1, le=1000)

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if not self.model_dump(exclude_none=True):
            raise ValueError("policy changes required")
        return self


class OrgPolicy(Contract):
    retention_days: int = 365
    max_upload_bytes: int = 10485760
    max_projects: int = 100
    version: int = 1
    etag: str = '"1"'
    source_license_enforcement: Literal[True] = True
    scientific_validation_required: Literal[True] = True
