"""Purpose: define staged object publication without coupling application logic to S3.
Behavior: expose upload, verification, abort and scoped download operations.
Refs: API section 3.3; main plan P02.5; Phase: P02.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class StagedObject:
    object_key: str
    upload_url: str
    expires_in_seconds: int


@dataclass(frozen=True, slots=True)
class VerifiedObject:
    object_key: str
    version_id: str | None
    size_bytes: int
    sha256: str


class ObjectStore(Protocol):
    def stage(
        self,
        tenant: str,
        project: str,
        upload: str,
        filename: str,
        sha256: str,
        media_type: str,
        expires_in: int = 300,
    ) -> StagedObject: ...
    def verify(
        self, object_key: str, expected_size: int, expected_sha256: str
    ) -> VerifiedObject: ...
    def finalize(self, verified: VerifiedObject) -> VerifiedObject: ...
    def abort(self, object_key: str) -> None: ...
    def download_url(
        self, object_key: str, authorized_tenant: str, expires_in: int = 300
    ) -> str: ...


class ValidatingObjectStore(ObjectStore, Protocol):
    def stage_checked(
        self,
        tenant: str,
        project: str,
        upload: str,
        filename: str,
        sha256: str,
        media_type: str,
        size: int,
    ) -> StagedObject: ...
    def read_checked(self, key: str, size: int, digest: str) -> bytes: ...
    def publish(self, key: str, body: bytes, digest: str, media_type: str) -> VerifiedObject: ...
