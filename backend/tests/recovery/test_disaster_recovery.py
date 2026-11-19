"""Purpose: exercise the P14.2 restore and worker cutover against disposable services.
Behavior: preserve native MinIO versions and replay a checkpoint from a PostgreSQL archive.
Refs: main implementation plan P14.2; docs/04_database.md §§6–7,9; Phase: P14.
"""

import hashlib
import json
import os
import secrets
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from time import perf_counter, sleep
from uuid import uuid4

import boto3
import psycopg
import pytest
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from p05_queue import approved_queue
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy import text

from orca.application.catalog import CatalogService
from orca.application.ingestion import preserve_snapshot
from orca.application.snapshots import SnapshotRepository
from orca.contracts.catalog import ProductVersion, ScientificProduct
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.jobs.postgres_queue import PostgresQueue
from orca.infrastructure.recovery.restore_verify import verify_object
from orca.infrastructure.storage.snapshot_s3 import SnapshotS3

pytestmark = [pytest.mark.live, pytest.mark.enable_socket]
BACKEND = Path(__file__).resolve().parents[2]
BUCKET = "orca-restore-drill"
STAGE_DIGEST = "sha256:" + "1" * 64


def command(*args, env=None, check=True):
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=90, check=False)
    if check and result.returncode:
        # Never copy credential-bearing service output into a test report.
        pytest.fail(f"{Path(args[0]).name} exited with {result.returncode}")
    return result


def database(name, prefix):
    url = os.environ.get(name)
    if not url:
        pytest.skip(f"Set {name} to a disposable loopback database")
    conn = conninfo_to_dict(url)
    assert conn["host"] == "127.0.0.1" and conn.get("port")
    assert conn["dbname"].startswith(prefix) and conn["dbname"].endswith("_test")
    assert "password" not in conn
    return url, conn


def minio(stack, image, volume):
    name = f"orca-p14-minio-{uuid4().hex}"
    access, secret = "orca" + secrets.token_hex(8), secrets.token_urlsafe(28)
    env = dict(os.environ, MINIO_ROOT_USER=access, MINIO_ROOT_PASSWORD=secret)
    command(
        "docker",
        "run",
        "--detach",
        "--name",
        name,
        "--publish",
        "127.0.0.1::9000",
        "--mount",
        f"type=volume,src={volume},dst=/data",
        "-e",
        "MINIO_ROOT_USER",
        "-e",
        "MINIO_ROOT_PASSWORD",
        image,
        "server",
        "/data",
        env=env,
    )
    stack.callback(command, "docker", "rm", "--force", name, check=False)
    port = command("docker", "port", name, "9000/tcp").stdout.strip().rsplit(":", 1)[1]
    endpoint = f"http://127.0.0.1:{int(port)}"
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name="us-east-1",
        config=Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1}),
    )
    stack.callback(client.close)
    for _ in range(40):
        try:
            client.list_buckets()
            return name, client, endpoint, access, secret
        except (BotoCoreError, ClientError):
            sleep(0.25)
    pytest.fail("Disposable object store did not become ready")


def verify_cli(env, *options):
    result = command(sys.executable, str(BACKEND / "scripts/restore_verify.py"), *options, env=env)
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def restored(tmp_path_factory):
    image = os.environ.get("ORCA_TEST_RESTORE_IMAGE")
    tools_image = os.environ.get("ORCA_TEST_RESTORE_TOOL_IMAGE")
    pg_bin = os.environ.get("ORCA_TEST_PG_BIN")
    if not image or not tools_image or not pg_bin:
        pytest.skip("Set both restore image IDs and ORCA_TEST_PG_BIN for the restore drill")
    assert all(item.startswith("sha256:") and len(item) == 71 for item in (image, tools_image))
    source_url, source_conn = database("ORCA_TEST_DATABASE_URL", "orca_p14_source_")
    _, source_runtime = database("ORCA_TEST_RUNTIME_DATABASE_URL", "orca_p14_source_")
    target_url, target_conn = database("ORCA_TEST_RESTORE_DATABASE_URL", "orca_p14_restore_")
    runtime_url, target_runtime = database("ORCA_TEST_RESTORE_RUNTIME_URL", "orca_p14_restore_")
    assert source_conn["port"] != target_conn["port"]
    assert (source_conn["port"], source_conn["dbname"]) == (
        source_runtime["port"],
        source_runtime["dbname"],
    )
    assert (target_conn["port"], target_conn["dbname"]) == (
        target_runtime["port"],
        target_runtime["dbname"],
    )
    with psycopg.connect(target_url) as connection:
        assert connection.execute("SELECT to_regclass('alembic_version')").fetchone() == (None,)
        assert connection.execute(
            "SELECT count(*) FROM pg_namespace WHERE nspname IN ('iam','research','federation')"
        ).fetchone() == (0,)

    folder = tmp_path_factory.mktemp("p14-restore")
    capture = str(folder / "references.json")
    backup = str(folder / "database.dump")
    env = dict(os.environ, ORCA_RESTORE_DATABASE_URL=source_url)
    started = perf_counter()
    with ExitStack() as stack:
        volumes = [f"orca-p14-{uuid4().hex}" for _ in range(3)]
        for volume in volumes:
            command("docker", "volume", "create", volume)
            stack.callback(command, "docker", "volume", "rm", volume, check=False)
        source_name, source, _, _, _ = minio(stack, image, volumes[0])
        source.create_bucket(Bucket=BUCKET)
        source.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        db, tenant, project, _, approved = approved_queue()
        stack.callback(db.dispose)
        content = b"test-only retained upload version"
        digest = hashlib.sha256(content).hexdigest()
        key = f"final/{tenant}/source/{digest}/fixture.csv"
        version = source.put_object(Bucket=BUCKET, Key=key, Body=content)["VersionId"]
        latest = b"test-only later upload version!"
        assert source.put_object(Bucket=BUCKET, Key=key, Body=latest)["VersionId"] != version
        with TenantUnitOfWork(db, tenant) as session:
            session.execute(
                text("""INSERT INTO research.uploads
                    (id,tenant_id,project_id,object_key,filename,media_type,size_bytes,sha256,
                     classification,status,object_version_id,finalized)
                    VALUES(:id,:t,:p,:k,'fixture.csv','text/csv',:n,:digest,
                           'restricted','finalized',:version,true)"""),
                {
                    "id": uuid4(),
                    "t": tenant,
                    "p": project,
                    "k": key,
                    "n": len(content),
                    "digest": digest,
                    "version": version,
                },
            )
            product = ScientificProduct(
                id=uuid4(),
                tenant_id=tenant,
                provider_id="restore-drill",
                source_product_id="test-only",
                kind="observation",
            )
            pinned = ProductVersion(
                id=uuid4(),
                tenant_id=tenant,
                product_id=product.id,
                source_version="test-only",
                metadata_digest="sha256:" + "a" * 64,
                native_variable="sst",
                standard_name="sea_surface_temperature",
                physical_definition="test-only",
                native_units="K",
                calendar="gregorian",
                retrieved_at="2025-02-01T00:00:00Z",
                license={
                    "license_ref": "test-only",
                    "evidence_digest": "sha256:" + "b" * 64,
                    "can_preserve": True,
                },
            )
            catalog = CatalogService(session, tenant)
            catalog.add_product(product)
            catalog.add_version(pinned)
            snapshot = preserve_snapshot(
                SnapshotS3(source, BUCKET),
                tenant,
                pinned,
                b"test-only snapshot bytes",
                origin_uri="https://example.org/test-only.nc",
                retrieved_at=pinned.retrieved_at,
            )
            SnapshotRepository(session, tenant).add(snapshot)
            queue = PostgresQueue(session, tenant, project)
            receipt = queue.enqueue_run(approved)
            old = queue.claim(lease_seconds=1)
            assert old is not None and old.run_id == receipt.run_id
            checkpoint = queue.checkpoint(old, "source-metadata", STAGE_DIGEST, STAGE_DIGEST)
            assert checkpoint is not None

        backup_started = perf_counter()
        captured = verify_cli(env, "capture", capture)
        assert captured["object_count"] == 2
        command(str(Path(pg_bin) / "pg_dump"), "--format=custom", "--file", backup, source_url)
        command("docker", "stop", source_name)
        command(
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "/bin/sh",
            "--mount",
            f"type=volume,src={volumes[0]},dst=/source,readonly",
            "--mount",
            f"type=volume,src={volumes[1]},dst=/backup",
            tools_image,
            "-ec",
            "tar -C /source -cf /backup/minio.tar .",
        )
        backup_seconds = round(perf_counter() - backup_started, 3)
        with psycopg.connect(source_url) as connection:
            name = connection.execute(
                "SELECT name FROM research.projects WHERE id=%s", (project,)
            ).fetchone()[0]
            assert (
                connection.execute(
                    "UPDATE research.projects SET name='committed after backup' WHERE id=%s",
                    (project,),
                ).rowcount
                == 1
            )
        db.dispose()
        with psycopg.connect(source_url, dbname="postgres", autocommit=True) as connection:
            connection.execute(
                sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(
                    sql.Identifier(source_conn["dbname"])
                )
            )
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(source_url, connect_timeout=3)

        restore_started = perf_counter()
        command(
            str(Path(pg_bin) / "pg_restore"),
            "--single-transaction",
            "--exit-on-error",
            "--dbname",
            target_url,
            backup,
        )
        command(
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "/bin/sh",
            "--mount",
            f"type=volume,src={volumes[1]},dst=/backup,readonly",
            "--mount",
            f"type=volume,src={volumes[2]},dst=/target",
            tools_image,
            "-ec",
            'test -z "$(ls -A /target)" && tar -C /target -xf /backup/minio.tar',
        )
        _, store, endpoint, access, secret = minio(stack, image, volumes[2])
        env.update(
            ORCA_RESTORE_DATABASE_URL=target_url,
            ORCA_RESTORE_S3_ENDPOINT=endpoint,
            ORCA_RESTORE_S3_BUCKET=BUCKET,
            ORCA_RESTORE_S3_ACCESS_KEY=access,
            ORCA_RESTORE_S3_SECRET_KEY=secret,
            ORCA_RESTORE_S3_REGION="us-east-1",
        )
        verified = verify_cli(env, "verify", capture, "--expected-digest", captured["digest"])
        restore_seconds = round(perf_counter() - restore_started, 3)
        assert verified["manifest_digest"] == captured["digest"]
        target = create_runtime_engine(runtime_url)
        stack.callback(target.dispose)
        yield {
            "target": target,
            "tenant": tenant,
            "project": project,
            "old": old,
            "checkpoint": checkpoint,
            "store": store,
            "capture": json.loads(Path(capture).read_text(encoding="utf-8")),
            "verified": verified,
            "original_name": name,
            "backup_bytes": Path(backup).stat().st_size,
            "database_archive_sha256": hashlib.sha256(Path(backup).read_bytes()).hexdigest(),
            "backup_seconds": backup_seconds,
            "restore_seconds": restore_seconds,
            "drill_seconds": round(perf_counter() - started, 3),
            "latest": latest,
        }


def test_restore_to_clean_env(restored, record_property):
    verified = restored["verified"]
    assert verified["status"] == "REFERENCES_AND_OBJECT_BYTES_VERIFIED"
    assert verified["object_count"] == 2
    upload = next(row for row in restored["capture"]["objects"] if row["kind"] == "upload")
    body = restored["store"].get_object(Bucket=BUCKET, Key=upload["key"])["Body"]
    with body:
        assert body.read() == restored["latest"]
    with restored["target"].connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM research.projects")).scalar_one() == 0
    with TenantUnitOfWork(restored["target"], restored["tenant"]) as session:
        assert (
            session.execute(text("SELECT name FROM research.projects")).scalar_one()
            == (restored["original_name"])
        )
    for key in ("backup_bytes", "backup_seconds", "restore_seconds", "drill_seconds"):
        record_property(key, restored[key])
    record_property("verified_objects", verified["object_count"])
    record_property("reference_manifest_digest", verified["manifest_digest"])
    record_property("database_archive_sha256", restored["database_archive_sha256"])
    record_property("verification_seconds", verified["verification_seconds"])
    record_property("post_backup_changes_absent", 1)


def test_missing_object_version_detected(restored, record_property):
    store = restored["store"]
    upload = next(row for row in restored["capture"]["objects"] if row["kind"] == "upload")
    other_bucket = "p14-restore-missing-version"
    store.create_bucket(Bucket=other_bucket)
    store.put_bucket_versioning(Bucket=other_bucket, VersioningConfiguration={"Status": "Enabled"})
    store.put_object(Bucket=other_bucket, Key=upload["key"], Body=restored["latest"])
    with pytest.raises(ClientError) as rejected:
        verify_object(store, other_bucket, upload)
    assert rejected.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404
    record_property("missing_native_version_http_status", 404)


def test_db_failover_job_recovery(restored, record_property):
    old = restored["old"]
    started = perf_counter()
    with TenantUnitOfWork(restored["target"], restored["tenant"]) as session:
        queue = PostgresQueue(session, restored["tenant"], restored["project"])
        new = queue.claim()
        assert new is not None and new.run_id == old.run_id
        assert new.generation == old.generation + 1
        assert queue.checkpoint(old, "source-metadata", STAGE_DIGEST, STAGE_DIGEST) is None
        assert (
            queue.checkpoint(new, "source-metadata", STAGE_DIGEST, STAGE_DIGEST)
            == restored["checkpoint"]
        )
        assert not queue.finish_inconclusive(old, "recovery_drill")
        assert queue.finish_inconclusive(new, "recovery_drill")
    with TenantUnitOfWork(restored["target"], restored["tenant"]) as session:
        assert session.execute(text("SELECT state FROM research.compute_runs")).scalar_one() == (
            "INCONCLUSIVE"
        )
        assert (
            session.execute(
                text("SELECT count(*) FROM research.run_stage_checkpoints")
            ).scalar_one()
            == 1
        )
        assert (
            session.execute(
                text("SELECT reserved_amount FROM research.plan_budget_ledgers")
            ).scalar_one()
            == 0
        )
        assert PostgresQueue(session, restored["tenant"], restored["project"]).claim() is None
    record_property("worker_recovery_seconds", round(perf_counter() - started, 3))
    record_property("recovered_generation", new.generation)
