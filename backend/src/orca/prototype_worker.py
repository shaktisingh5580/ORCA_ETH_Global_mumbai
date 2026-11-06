"""Purpose: execute the hackathon's durable approved-plan queue against real preserved inputs.
Behavior: fence retries, verify source bytes, compute a deterministic result and save its report.
Refs: HACKATHON_IMPLEMENTATION_PLAN.md H02; ADR-R02; Phase: H02.
"""

import argparse
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from orca.application.prototype import digest_result, render_report
from orca.application.snapshots import SnapshotRepository
from orca.application.workspace_access import authorize_project
from orca.config import Settings
from orca.contracts.catalog import ProductVersion
from orca.domain import prototype_analysis, scientific_inputs
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.domain.scientific_inputs import NativeGrid, parse_native_month, qualify_metadata
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.tenant_context import assert_runtime_role
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.storage.composition import snapshot_store
from orca.infrastructure.storage.snapshot_s3 import SnapshotS3


def execute(db: Engine, store: SnapshotS3, tenant: UUID, identifier: UUID) -> bool:
    token = uuid4()
    with TenantUnitOfWork(db, tenant) as session:
        row = (
            session.execute(
                text("""SELECT * FROM research.prototype_runs WHERE tenant_id=:t AND id=:id
          AND (status='queued' OR (status='running' AND lease_until<clock_timestamp())) FOR UPDATE SKIP LOCKED"""),
                {"t": tenant, "id": identifier},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return False
        if row["attempts"] >= 3:
            session.execute(
                text(
                    "UPDATE research.prototype_runs SET status='failed',error_code='RETRY_LIMIT',progress='failed' WHERE tenant_id=:t AND id=:id"
                ),
                {"t": tenant, "id": identifier},
            )
            return True
        session.execute(
            text("""UPDATE research.prototype_runs SET status='running',progress='loading_inputs',
          attempts=attempts+1,worker_token=:token,lease_until=clock_timestamp()+interval '120 seconds',
          updated_at=clock_timestamp() WHERE tenant_id=:t AND id=:id"""),
            {"t": tenant, "id": identifier, "token": token},
        )
        job = dict(row)
    principal = Principal(
        job["created_by"], "trusted-run-worker", frozenset().union(*ROLE_SCOPES.values())
    )
    try:
        with TenantUnitOfWork(db, tenant) as session:
            authorize_project(session, tenant, job["project_id"], principal, "project:write")
        bundle = json.loads(store.read(tenant, job["bundle_digest"]))
        if bundle["schema_version"] != "orca.scientific-input-bundle/1.0" or bundle[
            "tenant_id"
        ] != str(tenant):
            raise ValueError("source bundle identity mismatch")
        version = ProductVersion.model_validate(bundle["version"])
        if not version.can_preserve_for(tenant) or not version.license.can_redistribute:
            raise PermissionError("source preservation and reuse rights required")
        metadata = qualify_metadata(json.loads(store.read(tenant, bundle["metadata_digest"])))
        west, south, east, north = bundle["bbox"]
        grid = NativeGrid(west, south, east, north, metadata.longitude_step, metadata.latitude_step)
        with TenantUnitOfWork(db, tenant) as session:
            authorize_project(session, tenant, job["project_id"], principal, "project:write")
            repository = SnapshotRepository(session, tenant)
            for member in bundle["members"]:
                manifest = repository.get(UUID(member["snapshot_id"]))
                if (
                    manifest is None
                    or manifest.product_version.id != version.id
                    or manifest.raw_content_digest != member["sha256"]
                ):
                    raise ValueError("input snapshot lineage differs")
                repository.grant_project(manifest.id, job["project_id"])
            session.execute(
                text(
                    "UPDATE research.prototype_runs SET progress='computing',updated_at=clock_timestamp() WHERE tenant_id=:t AND id=:id AND worker_token=:token"
                ),
                {"t": tenant, "id": identifier, "token": token},
            )

        def load(member: dict[str, Any]) -> tuple[int, Any]:
            raw = store.read(tenant, member["sha256"])
            return member["year"], parse_native_month(raw, member["year"], 1, grid, metadata)

        with ThreadPoolExecutor(max_workers=4) as pool:
            months = dict(pool.map(load, bundle["members"]))
        result = prototype_analysis.seasonal_anomaly(
            months[2025], tuple(months[y] for y in range(2003, 2023))
        )
        result.update(
            {
                "bundle_digest": job["bundle_digest"],
                "metadata_digest": bundle["metadata_digest"],
                "input_snapshots": [
                    {"year": m["year"], "id": m["snapshot_id"], "sha256": m["sha256"]}
                    for m in bundle["members"]
                ],
                "analysis_as_of": bundle["analysis_as_of"],
                "license_ref": version.license.license_ref,
                "redistribution_permitted": True,
                "method_code_digest": "sha256:"
                + hashlib.sha256(Path(prototype_analysis.__file__).read_bytes()).hexdigest(),
                "input_parser_digest": "sha256:"
                + hashlib.sha256(Path(scientific_inputs.__file__).read_bytes()).hexdigest(),
            }
        )
        digest = digest_result(result)
        with TenantUnitOfWork(db, tenant) as session:
            authorize_project(session, tenant, job["project_id"], principal, "project:write")
            question: str = session.execute(
                text(
                    "SELECT question FROM research.prototype_investigations WHERE tenant_id=:t AND id=:inv"
                ),
                {"t": tenant, "inv": job["investigation_id"]},
            ).scalar_one()
            session.execute(
                text("""UPDATE research.prototype_runs SET status='succeeded',progress='complete',
              result=CAST(:result AS jsonb),result_digest=:digest,report=:report,worker_token=NULL,
              lease_until=NULL,updated_at=clock_timestamp() WHERE tenant_id=:t AND id=:id AND worker_token=:token"""),
                {
                    "t": tenant,
                    "id": identifier,
                    "token": token,
                    "result": json.dumps(result, allow_nan=False),
                    "digest": digest,
                    "report": render_report(question, result, digest),
                },
            )
        return True
    except Exception as error:
        code = (
            "ACCESS_REVOKED"
            if isinstance(error, (PermissionError, LookupError))
            else "INPUT_OR_EXECUTION_FAILED"
        )
        with TenantUnitOfWork(db, tenant) as session:
            session.execute(
                text("""UPDATE research.prototype_runs SET status='failed',progress='failed',error_code=:code,
              worker_token=NULL,lease_until=NULL,updated_at=clock_timestamp() WHERE tenant_id=:t AND id=:id AND worker_token=:token"""),
                {"t": tenant, "id": identifier, "token": token, "code": code},
            )
        return True


def process_pending(db: Engine, store: SnapshotS3) -> int:
    with db.begin() as connection:
        assert_runtime_role(connection)
        jobs = list(connection.execute(text("SELECT * FROM iam.prototype_pending_runs()")))
    return sum(int(execute(db, store, UUID(str(t)), UUID(str(i)))) for t, i in jobs)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings(_env_file=args.env_file)  # type: ignore[call-arg]
    if not settings.prototype_enabled or not settings.database_url:
        raise SystemExit("Configure the enabled prototype before starting its worker")
    db = create_runtime_engine(settings.database_url)
    store = snapshot_store(settings)
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
