"""Purpose: measure bounded queue, SST, and snapshot recovery workloads.
Behavior: enforce preregistered local ceilings and record timings and durable outcomes.
Refs: main_implementation plan.md P13.3; api design.md §14; Phase: P13.
"""

import hashlib
import io
import json
import math
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from statistics import median
from threading import Barrier
from time import perf_counter
from uuid import uuid4

import boto3
import pytest
from acquire_scientific_inputs import BASELINE_YEARS, CURRENT_YEAR, DATASET, HOST, query_for
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber
from p05_queue import approved_queue, queue_command
from sqlalchemy import event, text

from orca.domain.prototype_analysis import seasonal_anomaly
from orca.domain.scientific_inputs import (
    NativeGrid,
    parse_native_month,
    qualify_metadata,
    qualify_seasonal_inputs,
)
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.jobs.postgres_queue import PostgresQueue, RunRateLimitExceeded
from orca.infrastructure.storage.snapshot_s3 import SnapshotS3

ROOT = Path(__file__).resolve().parents[3]
PROFILE = json.loads(
    (ROOT / "infra/observability/p13-benchmark-profile.json").read_text(encoding="utf-8")
)


def burst(enqueue, targets, record_property):
    barrier = Barrier(len(targets))

    def run(command):
        barrier.wait(timeout=10)
        start = perf_counter()
        return enqueue(command), 1000 * (perf_counter() - start)

    with ThreadPoolExecutor(max_workers=len(targets)) as executor:
        start = perf_counter()
        outcomes = list(executor.map(run, targets))
        wall = perf_counter() - start
    latencies = sorted(latency for _, latency in outcomes)
    p95 = latencies[math.ceil(0.95 * len(latencies)) - 1]
    record_property("attempt_ms", json.dumps([round(value, 3) for value in latencies]))
    record_property("p50_ms", round(median(latencies), 3))
    record_property("p95_ms", round(p95, 3))
    record_property("attempts_per_second", round(len(targets) / wall, 2))
    assert p95 <= PROFILE["queue"]["p95_ms_max"]
    return [receipt for receipt, _ in outcomes]


def assert_durable_state(session, expected, reserved, record_property):
    counts = {
        table: session.execute(text(f"SELECT count(*) FROM research.{table}")).scalar_one()
        for table in ("compute_runs", "run_budget_reservations", "job_queue", "job_outbox")
    }
    ledger = session.execute(
        text("SELECT limit_amount,reserved_amount FROM research.plan_budget_ledgers")
    ).one()
    reservation_total = session.execute(
        text("SELECT sum(amount) FROM research.run_budget_reservations WHERE state='reserved'")
    ).scalar_one()
    assert set(counts.values()) == {expected}
    assert ledger.limit_amount == Decimal(PROFILE["queue"]["approved_plan_usd"])
    assert ledger.reserved_amount == Decimal(reserved)
    assert reservation_total == ledger.reserved_amount
    record_property("durable_counts", json.dumps(counts, sort_keys=True))
    record_property("reserved_usd", reserved)


@pytest.mark.live
def test_concurrent_enqueues_exactly_once(record_property):
    db, tenant, project, investigation, command = approved_queue()
    queue = PROFILE["queue"]
    try:
        with TenantUnitOfWork(db, tenant) as session:
            session.execute(
                text("""INSERT INTO iam.organization_run_quotas (tenant_id,max_runs_per_minute)
                    VALUES (:tenant,:quota)"""),
                {"tenant": tenant, "quota": queue["test_quota_per_minute"]},
            )

        def enqueue(candidate):
            with TenantUnitOfWork(db, tenant) as session:
                return PostgresQueue(session, tenant, project).enqueue_run(candidate)

        # All contenders reach the actual tenant lock before any can acquire it.
        # A pre-lock read can therefore miss the same uncommitted idempotency key.
        contenders = Barrier(queue["concurrent_clients"])

        def synchronize_admission(_conn, _cursor, statement, _parameters, _context, _many):
            if (
                statement.startswith("SELECT id FROM iam.organizations")
                and "FOR UPDATE" in statement
            ):
                contenders.wait(timeout=10)

        event.listen(db, "before_cursor_execute", synchronize_admission)
        try:
            receipts = burst(enqueue, [command] * queue["concurrent_clients"], record_property)
        finally:
            event.remove(db, "before_cursor_execute", synchronize_admission)
        assert all(receipt.state == "queued" for receipt in receipts)
        assert len(set(receipts)) == queue["expected_committed_runs_per_shared_key"]
        with pytest.raises(RunRateLimitExceeded):
            enqueue(
                queue_command(investigation, command.plan_id, command.approved_digest, "distinct")
            )
        with TenantUnitOfWork(db, tenant) as session:
            assert_durable_state(session, 1, queue["requested_usd_per_run"], record_property)
    finally:
        db.dispose()


@pytest.mark.live
def test_budget_quota_spike(record_property):
    db, tenant, project, investigation, command = approved_queue()
    queue = PROFILE["queue"]
    try:

        def enqueue(candidate):
            try:
                with TenantUnitOfWork(db, tenant) as session:
                    return PostgresQueue(session, tenant, project).enqueue_run(candidate)
            except ValueError as error:
                assert str(error) == "approved plan budget is already reserved"
                return None

        targets = [
            queue_command(
                investigation,
                command.plan_id,
                command.approved_digest,
                f"budget-{index}",
                queue["requested_usd_per_run"],
            )
            for index in range(queue["concurrent_clients"])
        ]
        receipts = burst(enqueue, targets, record_property)
        accepted = [receipt for receipt in receipts if receipt is not None]
        assert len(set(accepted)) == len(accepted) == queue["expected_runs_under_budget_spike"]
        record_property("budget_rejections", receipts.count(None))
        with TenantUnitOfWork(db, tenant) as session:
            assert_durable_state(session, len(accepted), "4.00", record_property)
    finally:
        db.dispose()


@pytest.mark.live
def test_real_sst_volume_with_mem_budget(record_property):
    folder = ROOT / ".cache/science-inputs/mur-native-batch"
    if not (folder / "capture.json").is_file():
        pytest.skip("preserved real NOAA MUR batch from P03.4 unavailable")
    capture = json.loads((folder / "capture.json").read_text(encoding="utf-8"))
    assert capture["source_host"] == HOST and capture["dataset"] == DATASET
    assert capture["fixture"] is False
    raw_metadata = (folder / "metadata.json").read_bytes()
    metadata_digest = "sha256:" + hashlib.sha256(raw_metadata).hexdigest()
    assert metadata_digest == capture["metadata"]["sha256"]
    proof = qualify_metadata(json.loads(raw_metadata))
    grid = NativeGrid(68.45, 20.45, 68.55, 20.55, proof.longitude_step, proof.latitude_step)
    totals = []
    months = {}
    limits = PROFILE["sst"]
    tracemalloc.start()
    start = perf_counter()
    try:
        for year in (CURRENT_YEAR, *BASELINE_YEARS):
            file_start = perf_counter()
            source = capture["inputs"][str(year)]
            raw = (folder / source["file"]).read_bytes()
            assert "sha256:" + hashlib.sha256(raw).hexdigest() == source["sha256"]
            assert len(raw) == source["bytes"] and source["query"] == query_for(year)
            months[year] = parse_native_month(raw, year, 1, grid, proof)
            totals.append(
                {
                    "year": year,
                    "bytes": len(raw),
                    "sha256": source["sha256"],
                    "parse_ms": round(1000 * (perf_counter() - file_start), 3),
                }
            )
        qualify_start = perf_counter()
        current = months[CURRENT_YEAR]
        baseline = tuple(months[year] for year in BASELINE_YEARS)
        qualified = qualify_seasonal_inputs(current, baseline, BASELINE_YEARS)
        qualify_ms = 1000 * (perf_counter() - qualify_start)
        compute_start = perf_counter()
        result = seasonal_anomaly(current, baseline)
        compute_ms = 1000 * (perf_counter() - compute_start)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    elapsed = perf_counter() - start
    assert qualified == capture["qualification"]
    assert qualified["status"] == "INPUT_COMPATIBILITY_VERIFIED"
    assert result["scientific_status"] == "prototype_not_independently_validated"
    assert result["anomaly"] == pytest.approx(0.6234972791400754, rel=0, abs=1e-9)
    assert len(months) == limits["expected_months"]
    cells = sum(len(month.cells) for month in months.values())
    assert cells == limits["expected_cells"]
    record_property("metadata_sha256", metadata_digest)
    record_property("cells", cells)
    record_property("months", json.dumps(totals))
    record_property("peak_traced_python_bytes", peak)
    record_property("elapsed_seconds", round(elapsed, 3))
    record_property("qualification_ms", round(qualify_ms, 3))
    record_property("reduction_with_validation_ms", round(compute_ms, 3))
    record_property("reduction_method", result["method"])
    assert peak <= limits["peak_traced_python_bytes_max"]
    assert elapsed <= limits["elapsed_seconds_max"]


def test_s3_object_lifecycle_under_failure(record_property):
    content = b"P13 storage recovery fixture"
    tenant = uuid4()
    digest = hashlib.sha256(content).hexdigest()
    client = boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="fixture", aws_secret_access_key="fixture"
    )
    storage = SnapshotS3(client, "snapshots")
    path = {"Bucket": "snapshots", "Key": f"snapshots/{tenant}/sha256/{digest}"}
    with Stubber(client) as stub:
        stub.add_client_error("get_object", "NoSuchKey", http_status_code=404, expected_params=path)
        stub.add_response(
            "put_object",
            {},
            {
                **path,
                "Body": content,
                "ContentType": "application/octet-stream",
                "Metadata": {"sha256": digest},
            },
        )
        stub.add_client_error("get_object", "SlowDown", http_status_code=503, expected_params=path)
        for _ in range(2):
            stub.add_response(
                "get_object",
                {
                    "Body": StreamingBody(io.BytesIO(content), len(content)),
                    "ContentLength": len(content),
                },
                path,
            )

        start = perf_counter()
        with pytest.raises(ClientError) as failure:
            storage.preserve(tenant, content, permitted=True)
        assert failure.value.response["Error"]["Code"] == "SlowDown"
        retry_start = perf_counter()
        published = storage.preserve(tenant, content, permitted=True)
        assert published == f"sha256:{digest}"
        assert storage.read(tenant, published) == content
        stub.assert_no_pending_responses()
        record_property("elapsed_ms", round(1000 * (perf_counter() - start), 3))
        record_property("recovery_and_read_ms", round(1000 * (perf_counter() - retry_start), 3))
        record_property("verified_put_calls", 1)
        record_property("verified_recovered_snapshots", 1)
