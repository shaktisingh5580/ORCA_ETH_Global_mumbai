"""Purpose: preserve raw acquisition bytes in tenant-scoped S3-compatible cloud storage.
Behavior: derive keys from exact bytes and read back bytes to verify publication and reuse.
Refs: main plan P03.3; Supabase S3 compatibility documentation; Phase: P03.
"""

import hashlib
from typing import Any, Protocol
from uuid import UUID

from botocore.exceptions import ClientError


class SnapshotS3Client(Protocol):
    def put_object(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_object(self, **kwargs: Any) -> dict[str, Any]: ...


class SnapshotS3:
    def __init__(self, client: SnapshotS3Client, bucket: str):
        self.client, self.bucket = client, bucket
        self.max_bytes = 16 * 1024 * 1024

    def _read(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        stream = response["Body"]
        try:
            if int(response["ContentLength"]) > self.max_bytes:
                raise ValueError("stored snapshot exceeds limit")
            content: bytes = stream.read(self.max_bytes + 1)
            if len(content) > self.max_bytes:
                raise ValueError("stored snapshot exceeds limit")
            return content
        finally:
            stream.close()

    def preserve(self, tenant: UUID, content: bytes, *, permitted: bool) -> str:
        if not permitted:
            raise PermissionError("preservation rights required")
        if not content or len(content) > self.max_bytes:
            raise ValueError("empty or oversized snapshot")
        digest = hashlib.sha256(content).hexdigest()
        key = f"snapshots/{tenant}/sha256/{digest}"
        try:
            existing = self._read(key)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in {"404", "NoSuchKey", "NotFound"}:
                raise
        else:
            if existing != content:
                raise ValueError("existing snapshot is corrupt")
            return "sha256:" + digest
        # Concurrent writers of this content address can only write the same exact bytes.
        # This is application immutability, not provider object-lock or native versioning.
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content,
            ContentType="application/octet-stream",
            Metadata={"sha256": digest},
        )
        if self._read(key) != content:
            raise ValueError("snapshot read-back verification failed")
        return "sha256:" + digest
