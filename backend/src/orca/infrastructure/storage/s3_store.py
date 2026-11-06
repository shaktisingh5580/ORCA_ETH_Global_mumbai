"""Purpose: implement quarantined, version-aware object storage over the S3 API.
Behavior: scope keys by tenant/project, bind checksums, and cap signed URL lifetime.
Refs: API section 3.3; docs/04_database.md section 7; main plan P02.5; Phase: P02.
"""

import re
from pathlib import PurePath
from typing import Any, Protocol

from orca.application.ports.object_store import StagedObject, VerifiedObject


class S3Client(Protocol):
    def list_buckets(self) -> dict[str, Any]: ...
    def create_bucket(self, **kwargs: Any) -> dict[str, Any]: ...
    def put_bucket_versioning(self, **kwargs: Any) -> dict[str, Any]: ...
    def generate_presigned_url(self, operation: str, **kwargs: Any) -> str: ...
    def head_object(self, **kwargs: Any) -> dict[str, Any]: ...
    def copy_object(self, **kwargs: Any) -> dict[str, Any]: ...
    def delete_object(self, **kwargs: Any) -> dict[str, Any]: ...


class S3ObjectStore:
    def __init__(self, client: S3Client, bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    @staticmethod
    def _segment(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) or value in {".", ".."}:
            raise ValueError("unsafe object key segment")
        return value

    @staticmethod
    def _ttl(value: int) -> int:
        if not 1 <= value <= 300:
            raise ValueError("signed URL TTL must be between 1 and 300 seconds")
        return value

    def ensure_bucket(self) -> None:
        names = {entry["Name"] for entry in self.client.list_buckets().get("Buckets", [])}
        if self.bucket not in names:
            self.client.create_bucket(Bucket=self.bucket)
        self.client.put_bucket_versioning(
            Bucket=self.bucket, VersioningConfiguration={"Status": "Enabled"}
        )

    def stage(
        self,
        tenant: str,
        project: str,
        upload: str,
        filename: str,
        sha256: str,
        media_type: str,
        expires_in: int = 300,
    ) -> StagedObject:
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("lowercase SHA-256 required")
        name = self._segment(PurePath(filename).name)
        if name != filename or "/" in filename or "\\" in filename:
            raise ValueError("filename must not contain a path")
        key = "/".join(
            ("staging", self._segment(tenant), self._segment(project), self._segment(upload), name)
        )
        params: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "ContentType": media_type,
            "Metadata": {"sha256": sha256},
        }
        url = self.client.generate_presigned_url(
            "put_object", Params=params, ExpiresIn=self._ttl(expires_in), HttpMethod="PUT"
        )
        return StagedObject(key, url, expires_in)

    def verify(self, object_key: str, expected_size: int, expected_sha256: str) -> VerifiedObject:
        response = self.client.head_object(Bucket=self.bucket, Key=object_key)
        actual_size = int(response["ContentLength"])
        actual_sha256 = str(response.get("Metadata", {}).get("sha256", ""))
        if actual_size != expected_size or actual_sha256 != expected_sha256:
            raise ValueError("uploaded object checksum or size mismatch")
        return VerifiedObject(object_key, response.get("VersionId"), actual_size, actual_sha256)

    def abort(self, object_key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=object_key)

    def finalize(self, verified: VerifiedObject) -> VerifiedObject:
        if not verified.object_key.startswith("staging/"):
            raise ValueError("only verified staging objects can be finalized")
        final_key = "final/" + verified.object_key.removeprefix("staging/")
        response = self.client.copy_object(
            Bucket=self.bucket,
            Key=final_key,
            CopySource={"Bucket": self.bucket, "Key": verified.object_key},
            MetadataDirective="COPY",
        )
        delete_args: dict[str, Any] = {"Bucket": self.bucket, "Key": verified.object_key}
        if verified.version_id:
            delete_args["VersionId"] = verified.version_id
        self.client.delete_object(**delete_args)
        return VerifiedObject(
            final_key, response.get("VersionId"), verified.size_bytes, verified.sha256
        )

    def download_url(self, object_key: str, authorized_tenant: str, expires_in: int = 300) -> str:
        prefix = f"final/{self._segment(authorized_tenant)}/"
        if not object_key.startswith(prefix):
            raise PermissionError("only finalized objects may be downloaded")
        return str(
            self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": object_key},
                ExpiresIn=self._ttl(expires_in),
                HttpMethod="GET",
            )
        )
