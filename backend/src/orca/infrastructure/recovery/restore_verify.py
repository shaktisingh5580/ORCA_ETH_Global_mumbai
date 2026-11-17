"""Purpose: verify a P08 restored database against captured exact object references.
Behavior: compare canonical DB rows and stream pinned S3 bytes through SHA-256 checks.
Refs: main_implementation plan.md P08.2; docs/04_database.md §7; Phase: P08.
"""

import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any, Protocol

from psycopg import Connection

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_OBJECT_BYTES = 100 * 1024 * 1024


class RecoveryS3(Protocol):
    def get_object(self, **kwargs: Any) -> dict[str, Any]: ...


def capture_references(connection: Connection[Any]) -> dict[str, Any]:
    """Capture only persisted finalized references, never object credentials."""
    revision_row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    if revision_row is None:
        raise ValueError("restored database has no migration revision")
    revision = revision_row[0]
    objects: list[dict[str, Any]] = []
    uploads = connection.execute("""SELECT id,tenant_id,object_key,object_version_id,
      sha256,size_bytes FROM research.uploads WHERE finalized=true ORDER BY tenant_id,id""")
    for identifier, tenant, key, version, digest, size in uploads:
        if not version or not key.startswith(f"final/{tenant}/"):
            raise ValueError("finalized upload lacks a scoped exact object version")
        objects.append(
            {
                "kind": "upload",
                "id": str(identifier),
                "tenant_id": str(tenant),
                "key": key,
                "version": version,
                "digest": "sha256:" + digest,
                "size_bytes": size,
            }
        )
    snapshots = connection.execute("""SELECT id,tenant_id,raw_content_digest,byte_count
      FROM research.dataset_snapshots ORDER BY tenant_id,id""")
    for identifier, tenant, digest, size in snapshots:
        objects.append(
            {
                "kind": "snapshot",
                "id": str(identifier),
                "tenant_id": str(tenant),
                "key": f"snapshots/{tenant}/sha256/{digest[7:]}",
                "version": digest,
                "digest": digest,
                "size_bytes": size,
            }
        )
    objects.sort(key=lambda row: (row["kind"], row["tenant_id"], row["id"]))
    return {
        "schema": "orca.restore-references/1",
        "migration_revision": revision,
        "objects": objects,
    }


def canonical_bytes(manifest: dict[str, Any]) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def manifest_digest(manifest: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(manifest)).hexdigest()


def _chunks(stream: Any) -> Iterable[bytes]:
    while chunk := stream.read(1024 * 1024):
        if not isinstance(chunk, bytes):
            raise ValueError("object body is not bytes")
        yield chunk


def verify_object(client: RecoveryS3, bucket: str, reference: dict[str, Any]) -> None:
    """Read and hash exact native version, or the content-addressed fallback."""
    digest = reference["digest"]
    version = reference["version"]
    size = reference["size_bytes"]
    if (
        not _DIGEST.fullmatch(digest)
        or not isinstance(size, int)
        or not 0 < size <= _MAX_OBJECT_BYTES
    ):
        raise ValueError("reference digest or size is invalid")
    tenant = reference["tenant_id"]
    key = reference["key"]
    if reference["kind"] == "upload":
        if not key.startswith(f"final/{tenant}/"):
            raise ValueError("upload key escapes tenant")
    elif reference["kind"] == "snapshot":
        if key != f"snapshots/{tenant}/sha256/{digest[7:]}":
            raise ValueError("snapshot key differs from digest")
    else:
        raise ValueError("unknown object reference kind")
    if version.startswith("sha256:") and version != digest:
        raise ValueError("content-addressed version differs from digest")
    params = {"Bucket": bucket, "Key": key}
    if not version.startswith("sha256:"):
        params["VersionId"] = version
    response = client.get_object(**params)
    stream = response["Body"]
    try:
        if int(response["ContentLength"]) != size:
            raise ValueError("object size differs from restored reference")
        if "VersionId" in params and response.get("VersionId") != version:
            raise ValueError("object version differs from restored reference")
        hasher = hashlib.sha256()
        actual_size = 0
        for chunk in _chunks(stream):
            actual_size += len(chunk)
            if actual_size > size:
                raise ValueError("object body exceeds restored reference")
            hasher.update(chunk)
        if actual_size != size or "sha256:" + hasher.hexdigest() != digest:
            raise ValueError("object bytes differ from restored reference")
    finally:
        stream.close()


def verify_restore(
    connection: Connection[Any],
    client: RecoveryS3,
    bucket: str,
    manifest: dict[str, Any],
    expected_digest: str,
) -> dict[str, int | str]:
    if not _DIGEST.fullmatch(expected_digest) or manifest_digest(manifest) != expected_digest:
        raise ValueError("source reference manifest digest differs")
    if capture_references(connection) != manifest:
        raise ValueError("restored database references differ from captured source")
    for reference in manifest["objects"]:
        verify_object(client, bucket, reference)
    return {
        "status": "REFERENCES_AND_OBJECT_BYTES_VERIFIED",
        "object_count": len(manifest["objects"]),
        "manifest_digest": expected_digest,
    }
