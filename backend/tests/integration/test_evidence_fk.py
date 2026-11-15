"""Purpose: prove P04 evidence references cannot dangle and persisted manifests are append-only.
Behavior: exercise composite snapshot FKs and runtime write grants on isolated local Postgres only.
Refs: main_implementation plan.md P04.4; docs/04_database.md §§3,6,7; Phase: P04.
"""

import json
import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from orca.application.evidence import ScientificEvidence
from orca.contracts.science_evidence import ScienceEvidenceManifest, ScienceSnapshotInput


def _local_database():
    url = os.environ.get("ORCA_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("isolated local database unavailable")
    if "@127.0.0.1:" not in url or "/orca_test" not in url:
        pytest.fail("science evidence integration requires local disposable orca_test")
    return create_engine(url.replace("postgresql://", "postgresql+psycopg://", 1))


def _seed_project(connection) -> tuple[object, object]:
    tenant, project = uuid4(), uuid4()
    connection.execute(
        text(
            "INSERT INTO iam.organizations(id,tenant_id,name,slug) VALUES(:id,:tenant,'test',:slug)"
        ),
        {"id": uuid4(), "tenant": tenant, "slug": str(tenant)},
    )
    connection.execute(
        text(
            "INSERT INTO research.projects(id,tenant_id,slug,name) VALUES(:id,:tenant,:slug,'test')"
        ),
        {"id": project, "tenant": tenant, "slug": str(project)},
    )
    return tenant, project


def _manifest(tenant, project) -> ScienceEvidenceManifest:
    return ScienceEvidenceManifest.build(
        id=uuid4(),
        tenant_id=tenant,
        project_id=project,
        created_by="test-operator",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_mode="preserved_real",
        method_id="test.sst",
        method_version="1.0.0",
        method_code_digest="a" * 64,
        method_approval_status="draft",
        qc_policy_id="test-qc-v1",
        qc_policy_digest="b" * 64,
        recipe_digest="sha256:" + "c" * 64,
        result_digest="sha256:" + "d" * 64,
        result_status="insufficient_evidence",
        source_qualified=False,
        baseline_qualified=False,
        inputs=(
            ScienceSnapshotInput(
                snapshot_id=uuid4(),
                role="current",
                year=2025,
                raw_content_digest="sha256:" + "e" * 64,
                byte_count=100,
            ),
            ScienceSnapshotInput(
                snapshot_id=uuid4(),
                role="baseline",
                year=2003,
                raw_content_digest="sha256:" + "f" * 64,
                byte_count=99,
            ),
        ),
    )


@pytest.mark.live
def test_dangling_support_fk_rejected():
    engine = _local_database()
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            tenant, project = _seed_project(connection)
            connection.execute(text("SET LOCAL ROLE orca_runtime"))
            with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                session.execute(
                    text("SELECT set_config('orca.tenant_id',:tenant,true)"),
                    {"tenant": str(tenant)},
                )
                with pytest.raises(IntegrityError):
                    ScientificEvidence(session, tenant, "test-operator").persist(
                        _manifest(tenant, project)
                    )
            transaction.rollback()
    finally:
        engine.dispose()


@pytest.mark.live
def test_method_version_immutable():
    engine = _local_database()
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            tenant, project = _seed_project(connection)
            identifier = uuid4()
            connection.execute(text("SET LOCAL ROLE orca_runtime"))
            connection.execute(
                text("SELECT set_config('orca.tenant_id',:tenant,true)"),
                {"tenant": str(tenant)},
            )
            connection.execute(
                text("""INSERT INTO research.science_evidence_manifests
              (id,tenant_id,project_id,created_by,manifest,manifest_digest)
              VALUES(:id,:tenant,:project,'test-operator',CAST(:manifest AS jsonb),:digest)"""),
                {
                    "id": identifier,
                    "tenant": tenant,
                    "project": project,
                    "manifest": json.dumps({"method_version": "1.0.0"}),
                    "digest": "sha256:" + "a" * 64,
                },
            )
            with pytest.raises(DBAPIError):
                connection.execute(
                    text(
                        "UPDATE research.science_evidence_manifests SET manifest='{}' WHERE id=:id"
                    ),
                    {"id": identifier},
                )
            transaction.rollback()
    finally:
        engine.dispose()
