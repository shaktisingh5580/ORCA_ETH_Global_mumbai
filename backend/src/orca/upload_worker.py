"""Purpose: recover and validate durable pending uploads against private object storage.
Behavior: discover bounded work, lock tenant rows and retry transient storage failures safely.
Refs: API 3.3; main plan P02.5; Phase: P02 completion repair.
"""

import argparse
import logging
import time
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import Engine, text

from orca.application.files import Files
from orca.application.ports.object_store import ValidatingObjectStore
from orca.config import Settings
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.tenant_context import assert_runtime_role
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.storage.composition import upload_store

logger = logging.getLogger(__name__)


def process_pending(db: Engine, store: ValidatingObjectStore) -> int:
    with db.begin() as connection:
        assert_runtime_role(connection)
        pending = list(connection.execute(text("SELECT * FROM iam.pending_uploads()")))
    processed = 0
    for tenant_value, identifier_value in pending:
        tenant, identifier = UUID(str(tenant_value)), UUID(str(identifier_value))
        try:
            with TenantUnitOfWork(db, tenant) as session:
                processed += int(Files(session, tenant, store).validate(identifier))
        except (BotoCoreError, ClientError):
            # Do not log signed URLs, SDK request details or private object keys.
            logger.warning("Upload storage unavailable; pending validation retained for retry")
    return processed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings(_env_file=args.env_file)  # type: ignore[call-arg]
    if not settings.file_uploads_enabled or not settings.database_url:
        raise SystemExit("Enable configured project upload storage before starting the validator")
    db, store = create_runtime_engine(settings.database_url), upload_store(settings)
    try:
        while True:
            process_pending(db, store)
            if args.once:
                break
            time.sleep(2)
    finally:
        db.dispose()


if __name__ == "__main__":
    main()
