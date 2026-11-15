"""Purpose: verify P05 enqueue transaction and atomic plan-budget reservation behavior.
Behavior: prove rollback leaves no durable fragments and budget capacity cannot be overspent.
Refs: main_implementation plan.md P05.2; api design.md §§5.1, 18.1; Phase: P05.
"""

import pytest
from p05_queue import approved_queue, queue_command
from sqlalchemy import text

from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.jobs.postgres_queue import PostgresQueue


@pytest.mark.live
def test_enqueue_rollback_atomic() -> None:
    db, tenant, project, _, queued = approved_queue()
    try:
        with pytest.raises(RuntimeError):
            with TenantUnitOfWork(db, tenant) as session:
                PostgresQueue(session, tenant, project).enqueue_run(queued)
                raise RuntimeError("simulate process crash before commit")
        with TenantUnitOfWork(db, tenant) as session:
            counts = [
                session.execute(text(f"SELECT count(*) FROM research.{table}")).scalar_one()
                for table in ("compute_runs", "run_budget_reservations", "job_queue", "job_outbox")
            ]
            assert counts == [0, 0, 0, 0]
    finally:
        db.dispose()


@pytest.mark.live
def test_budget_reserve_atomic() -> None:
    db, tenant, project, investigation_id, queued = approved_queue()
    try:
        with TenantUnitOfWork(db, tenant) as session:
            first = PostgresQueue(session, tenant, project).enqueue_run(queued)
            assert first.state == "queued"
        with (
            pytest.raises(ValueError, match="already reserved"),
            TenantUnitOfWork(db, tenant) as session,
        ):
            PostgresQueue(session, tenant, project).enqueue_run(
                queue_command(
                    investigation_id, queued.plan_id, queued.approved_digest, "queue-b", "4.00"
                )
            )
    finally:
        db.dispose()
