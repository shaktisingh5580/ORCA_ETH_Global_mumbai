"""Purpose: verify P08 restore tooling rejects stale versions, changed bytes and DB drift.
Behavior: exercise captured references with in-memory S3 only; staging restore remains a gate.
Refs: main_implementation plan.md P08.2; docs/04_database.md §7; Phase: P08.
"""

import hashlib
import io
from typing import Any
from uuid import uuid4

import pytest

from orca.infrastructure.recovery.restore_verify import (
    capture_references,
    manifest_digest,
    verify_restore,
)


class Cursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def fetchone(self) -> tuple[Any, ...]:
        return self.rows[0]

    def __iter__(self):
        return iter(self.rows)


class Database:
    def __init__(self, upload_version: str) -> None:
        self.tenant, self.upload, self.snapshot = uuid4(), uuid4(), uuid4()
        self.upload_bytes = b"uploaded exact bytes"
        self.snapshot_bytes = b"preserved SST bytes"
        self.upload_version = upload_version

    def execute(self, query: str) -> Cursor:
        if "alembic_version" in query:
            return Cursor([("0033_run_quotas",)])
        if "research.uploads" in query:
            digest = hashlib.sha256(self.upload_bytes).hexdigest()
            return Cursor(
                [
                    (
                        self.upload,
                        self.tenant,
                        f"final/{self.tenant}/source/{digest}/data.csv",
                        self.upload_version,
                        digest,
                        len(self.upload_bytes),
                    )
                ]
            )
        if "research.dataset_snapshots" in query:
            digest = "sha256:" + hashlib.sha256(self.snapshot_bytes).hexdigest()
            return Cursor([(self.snapshot, self.tenant, digest, len(self.snapshot_bytes))])
        raise AssertionError("unexpected SQL")


class Objects:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.replace = False
        self.version = database.upload_version

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs["Key"].startswith("final/"):
            content = b"changed bytes" if self.replace else self.database.upload_bytes
            return {
                "Body": io.BytesIO(content),
                "ContentLength": len(content),
                "VersionId": self.version,
            }
        return {
            "Body": io.BytesIO(self.database.snapshot_bytes),
            "ContentLength": len(self.database.snapshot_bytes),
        }


def test_restore_manifest_requires_exact_versions_and_bytes() -> None:
    database = Database("native-version-7")
    objects = Objects(database)
    manifest = capture_references(database)  # type: ignore[arg-type]
    digest = manifest_digest(manifest)
    assert verify_restore(database, objects, "private", manifest, digest)["object_count"] == 2  # type: ignore[arg-type]
    objects.version = "native-version-6"
    with pytest.raises(ValueError, match="version differs"):
        verify_restore(database, objects, "private", manifest, digest)  # type: ignore[arg-type]
    objects.version = database.upload_version
    objects.replace = True
    with pytest.raises(ValueError, match="bytes differ|size differs"):
        verify_restore(database, objects, "private", manifest, digest)  # type: ignore[arg-type]
    objects.replace = False
    with pytest.raises(ValueError, match="manifest digest differs"):
        verify_restore(database, objects, "private", manifest, "sha256:" + "0" * 64)  # type: ignore[arg-type]
    database.upload_version = "native-version-8"
    with pytest.raises(ValueError, match="database references differ"):
        verify_restore(database, objects, "private", manifest, digest)  # type: ignore[arg-type]
