"""Purpose: describe quarantined uploads and separately authorized project file downloads.
Behavior: expose bounded sessions and finalized metadata without storage keys or credentials.
Refs: API 3.3; main plan P02.5; Phase: P02 completion repair.
"""

from typing import Literal
from uuid import UUID

from pydantic import Field

from orca.contracts.common import Contract, Digest, UTCDateTime

MAX_FILE_BYTES = 16 * 1024 * 1024


class UploadPart(Contract):
    number: Literal[1] = 1
    method: Literal["PUT"] = "PUT"
    url: str
    headers: dict[str, str]


class UploadSession(Contract):
    upload_id: UUID
    parts: list[UploadPart]
    expires_at: UTCDateTime


class CompleteUpload(Contract):
    sha256: Digest
    part_etags: list[str] = Field(default_factory=list, max_length=1)


class UploadStatus(Contract):
    upload_id: UUID
    project_id: UUID
    status: Literal["staging", "pending_validation", "finalized", "rejected", "aborted"]
    filename: str
    rejection_code: str | None = None


class ProjectFile(Contract):
    id: UUID
    project_id: UUID
    filename: str
    media_type: str
    size_bytes: int
    sha256: Digest
    classification: Literal["organization", "project", "restricted", "confidential"]
    created_at: UTCDateTime


class DownloadIntent(Contract):
    purpose: str = Field(min_length=1, max_length=500)


class EphemeralDownload(Contract):
    url: str
    expires_at: UTCDateTime
    sha256: Digest
