"""Purpose: define private invitations and version-bound, expiring project-read grants.
Behavior: require verified-email acceptance, explicit target resources and bounded time windows.
Refs: API 3.2; main plan P02; Phase: P02 completion repair.
"""

from datetime import datetime, timedelta, timezone
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from orca.contracts.common import Contract, ResourceRef, UTCDateTime
from orca.contracts.organizations import MemberRole


def future_bound(value: datetime) -> datetime:
    now = datetime.now(timezone.utc)
    if not now + timedelta(minutes=1) < value <= now + timedelta(days=7):
        raise ValueError("expiry must be between one minute and seven days from now")
    return value


class CreateInvitation(Contract):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    role: MemberRole
    expires_at: UTCDateTime

    @field_validator("email")
    @classmethod
    def normalized(cls, value: str) -> str:
        return value.casefold()


class Invitation(Contract):
    id: UUID
    organization_id: UUID
    email: str
    role: MemberRole
    expires_at: UTCDateTime
    delivery: Literal["operator_out_of_band"] = "operator_out_of_band"


class AcceptInvitation(Contract):
    accept: Literal[True]


class PrincipalRef(Contract):
    type: Literal["organization_member"] = "organization_member"
    id: UUID


class GrantAccess(Contract):
    resource_ref: ResourceRef
    principal_ref: PrincipalRef
    scopes: list[Literal["project:read"]] = Field(min_length=1, max_length=1)
    expires_at: UTCDateTime

    @model_validator(mode="after")
    def project_only(self) -> Self:
        if self.resource_ref.type != "research_project":
            raise ValueError("prototype grants support explicit research_project resources only")
        UUID(self.resource_ref.id)
        return self


class AccessGrant(GrantAccess):
    id: UUID
    version: int
    etag: str
    revoked: bool
