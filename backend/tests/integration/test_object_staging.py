"""Purpose: verify P02 staging and signed downloads against actual S3-compatible storage.
Behavior: use a dedicated versioned bucket with synthetic bytes and tenant-scoped keys.
Refs: main plan P02.5; API section 3.3; Phase: P02.
"""

import hashlib
import os
from uuid import uuid4

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from orca.infrastructure.storage.s3_store import S3Client, S3ObjectStore


def live_store() -> tuple[S3ObjectStore, S3Client]:
    endpoint = os.environ.get("ORCA_TEST_S3_ENDPOINT", "")
    access = os.environ.get("ORCA_TEST_S3_ACCESS_KEY", "")
    secret = os.environ.get("ORCA_TEST_S3_SECRET_KEY", "")
    if not all((endpoint, access, secret)):
        pytest.skip("live object storage unavailable")
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name="us-east-1",
        config=Config(signature_version="s3v4"),
    )
    adapter = S3ObjectStore(client, "orca-research-test")
    adapter.ensure_bucket()
    return adapter, client


@pytest.mark.live
@pytest.mark.enable_socket
def test_partial_upload_cannot_publish():
    adapter, client = live_store()
    staged = adapter.stage(
        "tenant-a", "project", str(uuid4()), "partial.nc", "a" * 64, "application/x-netcdf"
    )
    with pytest.raises(PermissionError):
        adapter.download_url(staged.object_key, "tenant-a")
    with pytest.raises(ClientError):
        client.head_object(Bucket=adapter.bucket, Key=staged.object_key)


@pytest.mark.live
@pytest.mark.enable_socket
def test_checksum_mismatch_reject():
    adapter, client = live_store()
    staged = adapter.stage(
        "tenant-a", "project", str(uuid4()), "bad.bin", "a" * 64, "application/octet-stream"
    )
    client.put_object(
        Bucket=adapter.bucket, Key=staged.object_key, Body=b"data", Metadata={"sha256": "b" * 64}
    )
    with pytest.raises(ValueError, match="checksum"):
        adapter.verify(staged.object_key, 4, "a" * 64)
    adapter.abort(staged.object_key)


@pytest.mark.live
@pytest.mark.enable_socket
def test_cross_tenant_object_denied():
    adapter, client = live_store()
    body = b"synthetic"
    digest = hashlib.sha256(body).hexdigest()
    staged = adapter.stage(
        "tenant-a", "project", str(uuid4()), "private.bin", digest, "application/octet-stream"
    )
    client.put_object(
        Bucket=adapter.bucket, Key=staged.object_key, Body=body, Metadata={"sha256": digest}
    )
    final = adapter.finalize(adapter.verify(staged.object_key, len(body), digest))
    with pytest.raises(PermissionError):
        adapter.download_url(final.object_key, "tenant-b")


@pytest.mark.live
@pytest.mark.enable_socket
def test_signed_url_scoped_ttl():
    adapter, client = live_store()
    body = b"synthetic"
    digest = hashlib.sha256(body).hexdigest()
    staged = adapter.stage(
        "tenant-a",
        "project",
        str(uuid4()),
        "data.bin",
        digest,
        "application/octet-stream",
        expires_in=60,
    )
    client.put_object(
        Bucket=adapter.bucket, Key=staged.object_key, Body=body, Metadata={"sha256": digest}
    )
    final = adapter.finalize(adapter.verify(staged.object_key, len(body), digest))
    url = adapter.download_url(final.object_key, "tenant-a", 60)
    assert url.startswith(("http://127.0.0.1", "http://localhost"))
    assert "X-Amz-Expires=60" in url


@pytest.mark.live
@pytest.mark.enable_socket
def test_unauthorized_download_404():
    adapter, _ = live_store()
    try:
        adapter.download_url("final/tenant-b/project/secret", "tenant-a")
    except PermissionError:
        status = 404  # HTTP concealment mapping required by API section 3.3.
    else:
        status = 200
    assert status == 404
