"""Purpose: prove staged object checksums, tenant keys and signed URL restrictions.
Behavior: exercise the S3 adapter through a recording client without external I/O.
Refs: main plan P02.5; API section 3.3; Phase: P02.
"""

from typing import Any, cast

import pytest

from orca.application.ports.object_store import VerifiedObject
from orca.infrastructure.storage.s3_store import S3Client, S3ObjectStore


class FakeS3:
    def __init__(self) -> None:
        self.head: dict[str, Any] = {
            "ContentLength": 4,
            "Metadata": {"sha256": "a" * 64},
            "VersionId": "v1",
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_buckets(self) -> dict[str, Any]:
        return {"Buckets": [{"Name": "orca-research"}]}

    def create_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create_bucket", kwargs))
        return {}

    def put_bucket_versioning(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("put_bucket_versioning", kwargs))
        return {}

    def generate_presigned_url(self, operation: str, **kwargs: Any) -> str:
        self.calls.append((operation, kwargs))
        return "http://127.0.0.1/signed"

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        return self.head

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("delete_object", kwargs))
        return {}

    def copy_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("copy_object", kwargs))
        return {}


@pytest.fixture
def store() -> tuple[S3ObjectStore, FakeS3]:
    fake = FakeS3()
    return S3ObjectStore(cast(S3Client, fake), "orca-research"), fake


def test_partial_upload_unit_cannot_publish(store):
    adapter, fake = store
    staged = adapter.stage(
        "tenant", "project", "upload", "data.nc", "a" * 64, "application/x-netcdf"
    )
    assert staged.object_key.startswith("staging/tenant/project/upload/")
    with pytest.raises(PermissionError):
        adapter.download_url(staged.object_key, "tenant")
    fake.head = {"ContentLength": 3, "Metadata": {"sha256": "a" * 64}}
    with pytest.raises(ValueError):
        adapter.verify(staged.object_key, 4, "a" * 64)


def test_checksum_mismatch_unit_reject(store):
    adapter, fake = store
    fake.head = {"ContentLength": 4, "Metadata": {"sha256": "b" * 64}}
    with pytest.raises(ValueError, match="checksum"):
        adapter.verify("staging/t/p/u/data.nc", 4, "a" * 64)


def test_cross_tenant_object_unit_denied(store):
    adapter, _ = store
    for value in ("../tenant", "tenant/other", "..", ""):
        with pytest.raises(ValueError):
            adapter.stage(
                value, "project", "upload", "data.nc", "a" * 64, "application/octet-stream"
            )
    with pytest.raises(ValueError):
        adapter.stage("tenant", "project", "upload", "../secret", "a" * 64, "text/plain")


def test_signed_url_unit_scoped_ttl(store):
    adapter, fake = store
    url = adapter.download_url("final/tenant/project/file/data.nc", "tenant", 60)
    assert url == "http://127.0.0.1/signed"
    operation, kwargs = fake.calls[-1]
    assert operation == "get_object" and kwargs["ExpiresIn"] == 60
    assert kwargs["Params"]["Key"] == "final/tenant/project/file/data.nc"
    for ttl in (0, 301):
        with pytest.raises(ValueError):
            adapter.download_url("final/tenant/a", "tenant", ttl)


def test_unauthorized_download_unit_concealed(store):
    adapter, _ = store
    # The HTTP layer maps this concealed authorization failure to 404.
    with pytest.raises(PermissionError):
        adapter.download_url("final/other-tenant/private", "tenant")


def test_supabase_compatible_bucket_skips_native_versioning(store):
    adapter, fake = store
    adapter.ensure_bucket()
    assert not any(name == "put_bucket_versioning" for name, _ in fake.calls)


def test_minio_can_enable_native_versioning():
    fake = FakeS3()
    adapter = S3ObjectStore(cast(S3Client, fake), "orca-research", native_versioning=True)
    adapter.ensure_bucket()
    assert any(name == "put_bucket_versioning" for name, _ in fake.calls)


def test_existing_digest_object_finalizes_idempotently(store):
    adapter, fake = store
    verified = VerifiedObject("staging/tenant/project/upload/data.nc", "v0", 4, "a" * 64)
    final = adapter.finalize(verified)
    assert final.object_key == f"final/tenant/project/upload/{'a' * 64}/data.nc"
    assert final.version_id == "v1"
    assert not any(name == "copy_object" for name, _ in fake.calls)
    assert any(name == "delete_object" for name, _ in fake.calls)
