"""Purpose: perform durable institution service qualification outside HTTP/database transactions.
Behavior: fence leases, recheck current org authority and bind retries to one provider operation identity.
Refs: institutional phase F02.5; existing worker lease patterns; Phase: F02.
"""

import argparse
import json
import time
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from orca.application.agent_onboarding import Onboarding
from orca.config import Settings
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.federation.qualification import qualify
from orca.infrastructure.federation.runtime import NetworkRuntime, load_runtime


def execute(db: Engine, network: NetworkRuntime, tenant: UUID, identifier: UUID) -> bool:
    token = uuid4()
    with TenantUnitOfWork(db, tenant) as session:
        selected = (
            session.execute(
                text("""SELECT * FROM federation.registration_jobs
          WHERE tenant_id=:t AND id=:id AND (state='queued' OR (state='running' AND lease_until<now()))
          FOR UPDATE SKIP LOCKED"""),
                {"t": tenant, "id": identifier},
            )
            .mappings()
            .one_or_none()
        )
        if selected is None:
            return False
        job = dict(selected)
        session.execute(
            text("""UPDATE federation.registration_jobs SET state='running',lease_token=:token,
          lease_until=now()+interval '300 seconds' WHERE tenant_id=:t AND id=:id"""),
            {"t": tenant, "id": identifier, "token": token},
        )
    principal = Principal(
        job["actor"], "trusted-onboarding-worker", frozenset().union(*ROLE_SCOPES.values())
    )
    try:
        with TenantUnitOfWork(db, tenant) as session:
            app = Onboarding(session, tenant, job["organization_id"], principal)
            row = app.expected(job["agent_id"], job["expected_version"])
            if row["binding_version"] != job["binding_version"] or row["state"] != "verified":
                raise ValueError()
        binding = network.configuration.coordinator
        coordinator = {
            "id": str(binding.id),
            "key_id": binding.key_id,
            "public_key": binding.public_key,
        }
        report = qualify(row, coordinator, network.private_key, str(identifier))
        with TenantUnitOfWork(db, tenant) as session:
            current = session.execute(
                text("""SELECT id FROM federation.registration_jobs
              WHERE tenant_id=:t AND id=:id AND state='running' AND lease_token=:token
              AND lease_until>now() FOR UPDATE"""),
                {"t": tenant, "id": identifier, "token": token},
            ).first()
            if current is None:
                return True
            Onboarding(session, tenant, job["organization_id"], principal).qualify(
                job["agent_id"], job["expected_version"], job["binding_version"], report
            )
            session.execute(
                text("""UPDATE federation.registration_jobs SET state='succeeded',report=CAST(:r AS jsonb),
              lease_until=NULL WHERE tenant_id=:t AND id=:id AND lease_token=:token"""),
                {"t": tenant, "id": identifier, "token": token, "r": json.dumps(report)},
            )
    except Exception:
        with TenantUnitOfWork(db, tenant) as session:
            session.execute(
                text("""UPDATE federation.registration_jobs SET state='failed',
              error_code='QUALIFICATION_FAILED_OR_BINDING_CHANGED',lease_until=NULL
              WHERE tenant_id=:t AND id=:id AND state='running' AND lease_token=:token"""),
                {"t": tenant, "id": identifier, "token": token},
            )
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings(_env_file=args.env_file)  # type: ignore[call-arg]
    if not settings.database_url:
        raise ValueError("Database configuration required")
    db = create_runtime_engine(settings.database_url)
    network = load_runtime(settings)
    try:
        while True:
            with db.connect() as connection:
                jobs = connection.execute(
                    text("SELECT * FROM iam.pending_registration_jobs()")
                ).all()
            for tenant, identifier in jobs:
                execute(db, network, UUID(str(tenant)), UUID(str(identifier)))
            if args.once:
                break
            time.sleep(2)
    finally:
        db.dispose()


if __name__ == "__main__":
    main()
