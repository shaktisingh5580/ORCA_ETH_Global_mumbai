"""Purpose: verify raw snapshot preservation against an actual disposable S3 service.
Behavior: write exact test bytes, verify idempotent reuse and detect corrupted objects.
Refs: main plan P03.3; Supabase S3 compatibility; Phase: P03.
"""

import os
from uuid import uuid4

import boto3
import pytest
from botocore.config import Config

from orca.infrastructure.storage.snapshot_s3 import SnapshotS3


@pytest.mark.live
@pytest.mark.enable_socket
def test_cloud_snapshot_exact_bytes_and_reuse():
    endpoint = os.environ.get("ORCA_TEST_S3_ENDPOINT", "")
    if not endpoint:
        pytest.skip("isolated S3 service unavailable")
    if not endpoint.startswith("http://127.0.0.1:"):
        pytest.fail("cloud snapshot corruption test requires disposable local storage")
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["ORCA_TEST_S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["ORCA_TEST_S3_SECRET_KEY"],
        region_name="us-east-1",
        config=Config(signature_version="s3v4"),
    )
    bucket = "orca-snapshots-test"
    if bucket not in {b["Name"] for b in client.list_buckets()["Buckets"]}:
        client.create_bucket(Bucket=bucket)
    store, tenant = SnapshotS3(client, bucket), uuid4()
    content = b"test-only source\r\n"
    digest = store.preserve(tenant, content, permitted=True)
    assert store.preserve(tenant, content, permitted=True) == digest
    key = f"snapshots/{tenant}/sha256/{digest.removeprefix('sha256:')}"
    try:
        client.put_object(Bucket=bucket, Key=key, Body=b"corrupt")
        with pytest.raises(ValueError, match="corrupt"):
            store.preserve(tenant, content, permitted=True)
    finally:
        client.delete_object(Bucket=bucket, Key=key)
        client.close()
