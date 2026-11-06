"""Purpose: define bounded organization and membership lifecycle contracts.
Behavior: reject undeclared settings, require explicit versions and never infer institutional verification.
Refs: API 3.2; main plan P02; platform-completion-recovery.json; Phase: P02 repair.
"""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from orca.contracts.common import Contract, UTCDateTime

MemberRole = Literal[
    "org_owner",
    "org_admin",
    "principal_investigator",
    "researcher",
    "reviewer",
    "viewer",
    "source_operator",
]
MemberStatus = Literal["active", "suspended", "revoked"]


class OrganizationSettings(Contract):
    description: str | None = Field(default=None, max_length=4000)
    timezone: str = Field(default="UTC", min_length=1, max_length=128)


class CreateOrganization(Contract):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    visibility: Literal["private", "public"] = "private"


class PatchOrganization(Contract):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    settings: OrganizationSettings | None = None

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if self.display_name is None and self.settings is None:
            raise ValueError("organization changes required")
        return self


class OrganizationDetail(Contract):
    id: UUID
    name: str
    slug: str
    visibility: Literal["private", "public"]
    settings: OrganizationSettings
    version: int
    etag: str
    created_at: UTCDateTime
    updated_at: UTCDateTime
    verification_status: Literal["unverified"] = "unverified"


class Member(Contract):
    id: UUID
    organization_id: UUID
    subject: str
    role: MemberRole
    status: MemberStatus
    version: int
    etag: str


class PatchMember(Contract):
    role: MemberRole | None = None
    status: MemberStatus | None = None

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if self.role is None and self.status is None:
            raise ValueError("membership changes required")
        return self
