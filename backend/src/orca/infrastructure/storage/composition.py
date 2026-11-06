"""Purpose: construct bounded private S3 snapshot access for the accelerated research demo.
Behavior: use secret-backed configuration and avoid logging SDK exceptions or credentials.
Refs: accelerated-research-arkiv-demo.json; Phase: accelerated P04/P06 demo.
"""

from typing import cast

import boto3
from botocore.config import Config

from orca.config import Settings
from orca.infrastructure.storage.snapshot_s3 import SnapshotS3, SnapshotS3Client
from orca.infrastructure.storage.uploads import UploadClient, ValidatedUploadStore


def snapshot_store(settings: Settings) -> SnapshotS3:
    assert settings.object_store_access_key and settings.object_store_secret_key
    client = boto3.client(
        "s3",
        endpoint_url=settings.object_store_endpoint,
        aws_access_key_id=settings.object_store_access_key.get_secret_value(),
        aws_secret_access_key=settings.object_store_secret_key.get_secret_value(),
        region_name=settings.object_store_region,
        config=Config(
            signature_version="s3v4",
            connect_timeout=10,
            read_timeout=20,
            retries={"max_attempts": 2},
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )
    return SnapshotS3(cast(SnapshotS3Client, client), settings.object_store_bucket)


def upload_store(settings: Settings) -> ValidatedUploadStore:
    store = snapshot_store(settings)
    return ValidatedUploadStore(cast(UploadClient, store.client), store.bucket)
