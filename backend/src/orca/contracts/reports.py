"""Purpose: bound P06 report creation and immutable revision metadata.
Behavior: require evidence-bound report text and distinguish draft from published research.
Refs: main_implementation plan.md P06.2; api design.md §3.7; Phase: P06.
"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from orca.contracts.common import Contract, Digest, UTCDateTime


class CreateScientificReport(Contract):
    artifact_id: UUID
    title: str = Field(min_length=1, max_length=200)
    limitations: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=20
    )


class ScientificReportView(Contract):
    id: UUID
    investigation_id: UUID
    artifact_id: UUID
    revision: int
    created_by: str
    status: Literal["draft", "published"]
    document_digest: Digest
    html_digest: Digest
    pdf_digest: Digest
    created_at: UTCDateTime
    published_at: UTCDateTime | None
