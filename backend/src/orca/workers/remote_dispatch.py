"""Purpose: run P09 scoped external A2A research dispatches as a separate worker.
Behavior: poll durable intents with a private coordinator key and tenant-bound runtime role.
Refs: main_implementation plan.md P09.3; api design.md §17.1; Phase: P09.
"""

import argparse
import signal
from contextlib import AbstractContextManager
from threading import Event
from uuid import UUID

from sqlalchemy.orm import Session

from orca.application.remote_dispatch_worker import RemoteDispatchWorker
from orca.config import Settings
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.federation.coordinator import load_coordinator
from orca.workers.main import poll_project


def main() -> None:
    """Start one project worker with an explicit tenant and coordinator signing identity."""
    parser = argparse.ArgumentParser(description="Run ORCA external A2A research dispatches")
    parser.add_argument("--tenant", type=UUID, required=True)
    parser.add_argument("--project", type=UUID, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--idle-seconds", type=float, default=2.0)
    arguments = parser.parse_args()
    settings = Settings()  # type: ignore[call-arg]  # Read private runtime settings from env.
    if not settings.database_url:
        raise ValueError("remote dispatch worker requires a runtime database")
    coordinator = load_coordinator(settings)
    db = create_runtime_engine(settings.database_url)
    try:

        def transaction() -> AbstractContextManager[Session]:
            return TenantUnitOfWork(db, arguments.tenant)

        worker = RemoteDispatchWorker(arguments.tenant, arguments.project, transaction, coordinator)
        if arguments.once:
            worker.run_once()
            return
        stop = Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        poll_project(worker.run_once, stop, idle_seconds=arguments.idle_seconds)
    finally:
        db.dispose()


if __name__ == "__main__":
    main()
