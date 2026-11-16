"""Purpose: prove P05 PostgreSQL leases and fencing reject stale worker ownership.
Behavior: exercise real disposable database claims rather than an in-memory queue substitute.
Refs: main_implementation plan.md P05.2; api design.md §18.1; Phase: P05.
"""

import pytest
from p05_queue import approved_queue
from sqlalchemy import text

from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.jobs.postgres_queue import PostgresQueue


@pytest.mark.live
def test_two_workers_same_attempt_only_one_leader() -> None:
    db, tenant, project, _, queued = approved_queue()
    try:
        with TenantUnitOfWork(db, tenant) as session:
            receipt = PostgresQueue(session, tenant, project).enqueue_run(queued)
        with TenantUnitOfWork(db, tenant) as session:
            first = PostgresQueue(session, tenant, project).claim()
            assert first is not None and first.job_id == receipt.job_id
        with TenantUnitOfWork(db, tenant) as session:
            assert PostgresQueue(session, tenant, project).claim() is None
    finally:
        db.dispose()


@pytest.mark.live
def test_expired_lease_recovered() -> None:
    db, tenant, project, _, queued = approved_queue()
    try:
        with TenantUnitOfWork(db, tenant) as session:
            PostgresQueue(session, tenant, project).enqueue_run(queued)
            first = PostgresQueue(session, tenant, project).claim()
            assert first is not None
            session.execute(
                text(
                    "UPDATE research.job_queue SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id"
                ),
                {"id": first.job_id},
            )
        with TenantUnitOfWork(db, tenant) as session:
            recovered = PostgresQueue(session, tenant, project).claim()
            assert recovered is not None
            assert recovered.generation == first.generation + 1
            assert recovered.attempt_id != first.attempt_id
    finally:
        db.dispose()


@pytest.mark.live
def test_stale_generation_completion_rejected() -> None:
    db, tenant, project, _, queued = approved_queue()
    try:
        with TenantUnitOfWork(db, tenant) as session:
            PostgresQueue(session, tenant, project).enqueue_run(queued)
            first = PostgresQueue(session, tenant, project).claim()
            assert first is not None
            session.execute(
                text(
                    "UPDATE research.job_queue SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id"
                ),
                {"id": first.job_id},
            )
        with TenantUnitOfWork(db, tenant) as session:
            queue = PostgresQueue(session, tenant, project)
            second = queue.claim()
            assert second is not None
            assert queue.complete(first) is False
            assert queue.complete(second) is True
    finally:
        db.dispose()


@pytest.mark.live
def test_output_not_published_after_cancel() -> None:
    db, tenant, project, _, queued = approved_queue()
    try:
        with TenantUnitOfWork(db, tenant) as session:
            queue = PostgresQueue(session, tenant, project)
            receipt = queue.enqueue_run(queued)
            claimed = queue.claim()
            assert claimed is not None
        with TenantUnitOfWork(db, tenant) as session:
            assert PostgresQueue(session, tenant, project).cancel(receipt.job_id)
        with TenantUnitOfWork(db, tenant) as session:
            queue = PostgresQueue(session, tenant, project)
            assert not queue.finish_inconclusive(claimed, "late_worker")
            state = session.execute(
                text("SELECT state FROM research.compute_runs WHERE id=:run"),
                {"run": receipt.run_id},
            ).scalar_one()
            assert state == "CANCELLED"
            reserved = session.execute(
                text(
                    "SELECT reserved_amount FROM research.plan_budget_ledgers WHERE plan_id=:plan"
                ),
                {"plan": queued.plan_id},
            ).scalar_one()
            assert reserved == 0
    finally:
        db.dispose()


@pytest.mark.live
def test_retry_only_safe_errors() -> None:
    db, tenant, project, _, queued = approved_queue()
    try:
        with TenantUnitOfWork(db, tenant) as session:
            queue = PostgresQueue(session, tenant, project)
            receipt = queue.enqueue_run(queued)
            claimed = queue.claim()
            assert claimed is not None
            assert queue.finish_inconclusive(claimed, "source_or_approved_method_unavailable")
        with TenantUnitOfWork(db, tenant) as session:
            queue = PostgresQueue(session, tenant, project)
            assert not queue.retry(receipt.job_id)
            session.execute(
                text(
                    "UPDATE research.compute_runs SET stage='blocked:source_timeout' WHERE id=:run"
                ),
                {"run": receipt.run_id},
            )
            assert queue.retry(receipt.job_id)
            reserved = session.execute(
                text(
                    "SELECT reserved_amount FROM research.plan_budget_ledgers WHERE plan_id=:plan"
                ),
                {"plan": queued.plan_id},
            ).scalar_one()
            assert reserved == queued.execution_budget
    finally:
        db.dispose()


@pytest.mark.live
def test_worker_kill_each_durable_stage() -> None:
    db, tenant, project, _, queued = approved_queue()
    digest = "sha256:" + "1" * 64
    try:
        with TenantUnitOfWork(db, tenant) as session:
            queue = PostgresQueue(session, tenant, project)
            receipt = queue.enqueue_run(queued)
            first = queue.claim()
            assert first is not None
            checkpoint = queue.checkpoint(first, "source-metadata", digest, digest)
            assert checkpoint is not None
            session.execute(
                text(
                    "UPDATE research.job_queue SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:job"
                ),
                {"job": receipt.job_id},
            )
        with TenantUnitOfWork(db, tenant) as session:
            queue = PostgresQueue(session, tenant, project)
            recovered = queue.claim()
            assert recovered is not None and recovered.generation > first.generation
            assert queue.checkpoint(first, "source-metadata", digest, digest) is None
            assert queue.checkpoint(recovered, "source-metadata", digest, digest) == checkpoint
            with pytest.raises(ValueError, match="differs"):
                queue.checkpoint(recovered, "source-metadata", digest, "sha256:" + "2" * 64)
            count = session.execute(
                text("SELECT count(*) FROM research.run_stage_checkpoints WHERE run_id=:run"),
                {"run": receipt.run_id},
            ).scalar_one()
            assert count == 1
    finally:
        db.dispose()
