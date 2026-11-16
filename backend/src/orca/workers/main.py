"""Purpose: compose and run tenant-scoped P05 workers from live database and preserved storage.
Behavior: poll durable jobs with graceful stop; no synthetic fixtures or implicit method approval.
Refs: main_implementation plan.md P05.3-P05.5; Phase: P05.
"""

import argparse
import signal
from collections.abc import Callable
from contextlib import AbstractContextManager
from threading import Event
from uuid import UUID

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from orca.config import Settings
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.storage.composition import snapshot_store
from orca.workers.handlers.snapshot_evidence import (
    ProjectSnapshotReader,
    SnapshotInsufficiencyHandler,
)
from orca.workers.runner import RunWorker


def _compose(settings: Settings, tenant: UUID, project: UUID, db: Engine) -> RunWorker:
    """Construct one worker without opening a connection until a transaction starts."""
    if not settings.database_url or not all(
        (
            settings.object_store_endpoint,
            settings.object_store_access_key,
            settings.object_store_secret_key,
        )
    ):
        raise ValueError("worker requires non-owner database and private snapshot storage")
    trusted = {
        binding.version_id: f"https://{binding.host}/erddap/griddap/{binding.dataset}.csv"
        for binding in settings.acquisition_sources
        if binding.tenant_id == tenant
    }
    if not trusted:
        raise ValueError("worker requires explicit approved source bindings for the tenant")

    def transaction() -> AbstractContextManager[Session]:
        return TenantUnitOfWork(db, tenant)

    reader = ProjectSnapshotReader(tenant, project, transaction, snapshot_store(settings))
    handler = SnapshotInsufficiencyHandler(tenant, project, reader, trusted)
    return RunWorker(tenant, project, transaction, handler)


def run_project_once(settings: Settings, tenant: UUID, project: UUID) -> str:
    """Process one durable project job; the current handler can prove insufficiency only."""
    if not settings.database_url:
        raise ValueError("worker requires a runtime database")
    db = create_runtime_engine(settings.database_url)
    try:
        return _compose(settings, tenant, project, db).run_once()
    finally:
        db.dispose()


def poll_project(run_once: Callable[[], str], stop: Event, *, idle_seconds: float = 2.0) -> None:
    """Keep claiming until stopped; process queued work immediately after a completed attempt."""
    if not 0.1 <= idle_seconds <= 60:
        raise ValueError("poll interval must be between 0.1 and 60 seconds")
    while not stop.is_set():
        outcome = run_once()
        if outcome == "idle":
            stop.wait(idle_seconds)


def main() -> None:
    """Start a single-project worker with environment-based private runtime configuration."""
    parser = argparse.ArgumentParser(description="Run an ORCA tenant/project research worker")
    parser.add_argument("--tenant", type=UUID, required=True)
    parser.add_argument("--project", type=UUID, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--idle-seconds", type=float, default=2.0)
    arguments = parser.parse_args()
    settings = Settings()  # type: ignore[call-arg]  # BaseSettings reads the required token from env.
    if not settings.database_url:
        raise ValueError("worker requires a runtime database")
    db = create_runtime_engine(settings.database_url)
    try:
        worker = _compose(settings, arguments.tenant, arguments.project, db)
        if arguments.once:
            worker.run_once()
            return
        stop = Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        poll_project(worker.run_once, stop, idle_seconds=arguments.idle_seconds)
    finally:
        db.dispose()


__all__ = ["RunWorker", "run_project_once", "poll_project", "main"]


if __name__ == "__main__":
    main()
