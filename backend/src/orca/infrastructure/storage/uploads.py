"""Purpose: verify actual uploaded bytes and publish immutable validated copies over private S3.
Behavior: bound memory, hash streamed content and copy verified bytes rather than trusting metadata.
Refs: API 3.3; main plan P02.5; Phase: P02 completion repair.
"""

import hashlib
from typing import Any, Protocol

from orca.application.ports.object_store import StagedObject, VerifiedObject
from orca.contracts.files import MAX_FILE_BYTES
from orca.infrastructure.storage.s3_store import S3Client, S3ObjectStore


class UploadClient(S3Client, Protocol):
    def get_object(self, **kwargs: Any) -> dict[str, Any]: ...
    def put_object(self, **kwargs: Any) -> dict[str, Any]: ...


class ValidatedUploadStore(S3ObjectStore):
    client: UploadClient

    def __init__(self, client: UploadClient, bucket: str):
        super().__init__(client, bucket)
        self.client = client

    def stage_checked(
        self,
        tenant: str,
        project: str,
        upload: str,
        filename: str,
        sha256: str,
        media_type: str,
        size: int,
    ) -> StagedObject:
        if not 0 < size <= MAX_FILE_BYTES:
            raise ValueError("upload exceeds processing limit")
        key = "/".join(
            ["staging", *(self._segment(value) for value in (tenant, project, upload, filename))]
        )
        url = self.client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ContentType": media_type,
                "ContentLength": size,
                "Metadata": {"sha256": sha256},
            },
            ExpiresIn=300,
            HttpMethod="PUT",
        )
        return StagedObject(key, url, 300)

    def read_checked(self, key: str, size: int, digest: str) -> bytes:
        if size <= 0 or size > MAX_FILE_BYTES:
            raise ValueError("file exceeds processing limit")
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        stream = response["Body"]
        try:
            if int(response["ContentLength"]) != size:
                raise ValueError("uploaded size mismatch")
            body: bytes = stream.read(size + 1)
            if len(body) != size or hashlib.sha256(body).hexdigest() != digest:
                raise ValueError("uploaded checksum mismatch")
            return body
        finally:
            stream.close()

    def publish(self, key: str, body: bytes, digest: str, media_type: str) -> VerifiedObject:
        if not key.startswith("staging/") or hashlib.sha256(body).hexdigest() != digest:
            raise ValueError("invalid publication")
        final = "final/" + key.removeprefix("staging/") + "/" + digest
        # A still-live signed staging PUT can never change these exact verified bytes.
        self.client.put_object(
            Bucket=self.bucket,
            Key=final,
            Body=body,
            ContentType=media_type,
            Metadata={"sha256": digest},
            ContentDisposition="attachment",
        )
        self.read_checked(final, len(body), digest)
        return VerifiedObject(final, "sha256:" + digest, len(body), digest)

    def download_url(self, object_key: str, authorized_tenant: str, expires_in: int = 60) -> str:
        if not object_key.startswith(f"final/{self._segment(authorized_tenant)}/"):
            raise PermissionError("finalized tenant object required")
        return str(
            self.client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": self.bucket,
                    "Key": object_key,
                    "ResponseContentDisposition": "attachment",
                    "ResponseContentType": "application/octet-stream",
                },
                ExpiresIn=self._ttl(expires_in),
                HttpMethod="GET",
            )
        )
