"""Purpose: run the tenant-scoped P03 acquisition worker as a separate deployable process.
Behavior: compose approved real adapters, process durable requests and expose sanitized outcomes.
Refs: main plan P03.3/P03.5; API 7; Phase: P03.
"""

import argparse
import logging
import time
from typing import cast
from uuid import UUID

import boto3
from botocore.config import Config

from orca.application.acquisition_worker import (
    AcquisitionFailed,
    AcquisitionResult,
    AcquisitionWorker,
)
from orca.config import Settings
from orca.contracts.catalog import ProductVersion
from orca.contracts.snapshot_requests import SnapshotRequest
from orca.infrastructure.connectors.acquisition import ERDDAPAcquisition
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.storage.snapshot_s3 import SnapshotS3, SnapshotS3Client


class ConfiguredSources:
    def __init__(self, settings: Settings, tenant: UUID):
        self.adapters = {
            binding.version_id: ERDDAPAcquisition(binding)
            for binding in settings.acquisition_sources
            if binding.tenant_id == tenant
        }
        if not self.adapters:
            raise ValueError("no configured sources for worker tenant")

    def fetch(self, request: SnapshotRequest, version: ProductVersion) -> AcquisitionResult:
        adapter = self.adapters.get(version.id)
        if adapter is None:
            raise AcquisitionFailed("SOURCE_NOT_CONFIGURED")
        return adapter.fetch(request, version)


def main() -> int:
    parser = argparse.ArgumentParser(description="Process approved tenant acquisition requests")
    parser.add_argument("--tenant", type=UUID, required=True)
    parser.add_argument(
        "--watch", action="store_true", help="Poll until interrupted; default one request"
    )
    args = parser.parse_args()
    settings = Settings()  # type: ignore[call-arg]
    if not settings.acquisition_enabled:
        raise ValueError("acquisition is disabled")
    assert (
        settings.database_url
        and settings.object_store_access_key
        and settings.object_store_secret_key
    )
    engine = create_runtime_engine(settings.database_url)
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
    worker = AcquisitionWorker(
        args.tenant,
        lambda: TenantUnitOfWork(engine, args.tenant),
        ConfiguredSources(settings, args.tenant),
        SnapshotS3(cast(SnapshotS3Client, client), settings.object_store_bucket),
    )
    logging.basicConfig(level=logging.INFO)
    try:
        while True:
            try:
                outcome = worker.run_once()
            except Exception:
                # Never log connection URLs, provider payloads or credential-bearing SDK exceptions.
                logging.error("ACQUISITION_INFRASTRUCTURE_ERROR; lease remains recoverable")
                if not args.watch:
                    return 1
                time.sleep(10)
                continue
            logging.info("acquisition outcome=%s", outcome)
            if not args.watch:
                return 0 if outcome in {"idle", "succeeded"} else 1
            if outcome != "succeeded":
                time.sleep(5)
    except KeyboardInterrupt:
        return 0
    finally:
        client.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
