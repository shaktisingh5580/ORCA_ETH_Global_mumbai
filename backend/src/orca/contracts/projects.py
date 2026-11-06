"""Purpose: define strict P02 project creation and update request contracts.
Behavior: reject unknown fields and bound slugs, names and classifications.
Refs: API section 3.3; main plan P02.4; Phase: P02.
"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from orca.contracts.common import Contract, Digest


class CreateProject(Contract):
    organization_id: UUID
    slug: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    description: Annotated[str, Field(max_length=4000)] | None = None
    visibility: Literal["organization", "project", "restricted", "confidential"] = "project"


class PatchProject(Contract):
    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    description: Annotated[str, Field(max_length=4000)] | None = None
    visibility: Literal["organization", "project", "restricted", "confidential"] | None = None


class UploadIntent(Contract):
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    media_type: Annotated[str, Field(min_length=1, max_length=127)]
    size_bytes: Annotated[int, Field(gt=0, le=5_000_000_000)]
    sha256: Digest
    classification: Literal["organization", "project", "restricted", "confidential"]
