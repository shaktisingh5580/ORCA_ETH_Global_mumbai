"""Purpose: compose one tenant-scoped P05 worker from live database and preserved source storage.
Behavior: executes only approved-bound snapshot evidence; no synthetic fixtures or implicit method approval.
Refs: main_implementation plan.md P05.3-P05.5; Phase: P05.
"""

from uuid import UUID

from orca.config import Settings
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.storage.composition import snapshot_store
from orca.workers.handlers.snapshot_evidence import (
    ProjectSnapshotReader,
    SnapshotInsufficiencyHandler,
)
from orca.workers.runner import RunWorker


def run_project_once(settings: Settings, tenant: UUID, project: UUID) -> str:
    """Process one durable project job; the current handler can prove insufficiency only."""
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
    db = create_runtime_engine(settings.database_url)
    try:

        def transaction() -> TenantUnitOfWork:
            return TenantUnitOfWork(db, tenant)

        reader = ProjectSnapshotReader(tenant, project, transaction, snapshot_store(settings))
        handler = SnapshotInsufficiencyHandler(tenant, project, reader, trusted)
        return RunWorker(tenant, project, transaction, handler).run_once()
    finally:
        db.dispose()


__all__ = ["RunWorker", "run_project_once"]
