"""Purpose: capture and verify P08 database/object references around a staging restore.
Behavior: require environment-held credentials and fail closed on missing or changed bytes.
Refs: main_implementation plan.md P08.2; docs/04_database.md §7; Phase: P08.
"""

import argparse
import json
import os
from pathlib import Path
from typing import cast

import boto3
import psycopg
from botocore.config import Config

from orca.infrastructure.recovery.restore_verify import (
    RecoveryS3,
    canonical_bytes,
    capture_references,
    manifest_digest,
    verify_restore,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture or verify exact restored ORCA object references"
    )
    parser.add_argument("command", choices=["capture", "verify"])
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--expected-digest", default=None)
    args = parser.parse_args()
    url = os.environ.get("ORCA_RESTORE_DATABASE_URL", "")
    if not url:
        parser.error("ORCA_RESTORE_DATABASE_URL is required")
    try:
        if args.command == "capture":
            with psycopg.connect(url, connect_timeout=10) as connection:
                manifest = capture_references(connection)
            args.manifest.write_bytes(canonical_bytes(manifest) + b"\n")
            print(
                json.dumps(
                    {
                        "status": "CAPTURED",
                        "digest": manifest_digest(manifest),
                        "object_count": len(manifest["objects"]),
                    }
                )
            )
            return 0
        if not args.expected_digest:
            parser.error("--expected-digest from the source capture is required for verify")
        endpoint = os.environ.get("ORCA_RESTORE_S3_ENDPOINT", "")
        bucket = os.environ.get("ORCA_RESTORE_S3_BUCKET", "")
        access = os.environ.get("ORCA_RESTORE_S3_ACCESS_KEY", "")
        secret = os.environ.get("ORCA_RESTORE_S3_SECRET_KEY", "")
        region = os.environ.get("ORCA_RESTORE_S3_REGION", "")
        if not all((endpoint, bucket, access, secret, region)):
            parser.error("ORCA_RESTORE_S3_* configuration is required for verify")
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access,
            aws_secret_access_key=secret,
            region_name=region,
            config=Config(
                signature_version="s3v4",
                connect_timeout=10,
                read_timeout=20,
                retries={"max_attempts": 2},
                s3={"addressing_style": "path"},
            ),
        )
        with psycopg.connect(url, connect_timeout=10) as connection:
            result = verify_restore(
                connection, cast(RecoveryS3, client), bucket, manifest, args.expected_digest
            )
        print(json.dumps(result))
        return 0
    except Exception:
        # SDK and SQL exceptions may contain connection strings, bucket paths or credentials.
        print(json.dumps({"status": "RESTORE_VERIFICATION_FAILED"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
