"""Purpose: pin operator-approved ERDDAP distributions to tenant product versions.
Behavior: restrict worker sources and avoid accepting arbitrary download URLs from users.
Refs: main plan P03.2/P03.3; API 7; Phase: P03.
"""

from typing import Literal
from uuid import UUID

from pydantic import Field

from orca.contracts.common import Contract


class SourceBinding(Contract):
    tenant_id: UUID
    version_id: UUID
    host: Literal["coastwatch.pfeg.noaa.gov", "erddap.incois.gov.in"]
    dataset: str = Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
