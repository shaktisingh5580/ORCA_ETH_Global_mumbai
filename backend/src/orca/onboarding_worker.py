"""Purpose: perform durable institution service qualification outside HTTP/database transactions.
Behavior: fence leases, recheck current org authority and bind retries to one provider operation identity.
Refs: institutional phase F02.5; main_implementation plan.md P09.1-P09.3; Phase: F02/P09.
"""

import argparse
import json
import time
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
from sqlalchemy import Engine, text

from orca.application.agent_onboarding import (
    Onboarding,
    registered_endpoint,
    validate_registered_card,
)
from orca.config import Settings
from orca.contracts.network import AgentEndpointObservation
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.federation.coordinator import FederationCoordinator, load_coordinator
from orca.infrastructure.federation.https_egress import fetch_card
from orca.infrastructure.federation.qualification import qualify
from orca.infrastructure.federation.runtime import NetworkRuntime, load_runtime


def observe_endpoint(row: dict[str, Any], endpoint: str | None) -> dict[str, Any]:
    selected = registered_endpoint(row["registration"], endpoint)
    status: Literal["matches_registration", "card_mismatch", "unavailable"]
    try:
        card = fetch_card(selected.endpoint)
    except (OSError, ValueError, httpx.HTTPError):
        status = "unavailable"
    else:
        try:
            validate_registered_card(card, selected.model_dump(mode="json"))
        except ValueError:
            status = "card_mismatch"
        else:
            status = "matches_registration"
    return AgentEndpointObservation(
        agent_id=row["id"],
        binding_version=row["binding_version"],
        observed_at=datetime.now(timezone.utc),
        status=status,
        endpoint=selected.endpoint,
    ).model_dump(mode="json")


def execute(
    db: Engine, network: NetworkRuntime | FederationCoordinator, tenant: UUID, identifier: UUID
) -> bool:
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
            if row["binding_version"] != job["binding_version"] or (
                job["kind"] == "qualification" and row["state"] != "verified"
            ):
                raise ValueError()
        if job["kind"] == "endpoint_check":
            report = observe_endpoint(row, job["endpoint"])
        else:
            if isinstance(network, FederationCoordinator):
                coordinator = network.public_descriptor()
            else:
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
              AND lease_until>clock_timestamp() FOR UPDATE"""),
                {"t": tenant, "id": identifier, "token": token},
            ).first()
            if current is None:
                return True
            app = Onboarding(session, tenant, job["organization_id"], principal)
            if job["kind"] == "endpoint_check":
                latest = app.expected(job["agent_id"], job["expected_version"])
                if latest["binding_version"] != job["binding_version"]:
                    raise ValueError("Endpoint check binding has changed")
                app.event(job["agent_id"], "endpoint_checked", report)
            else:
                app.qualify(
                    job["agent_id"], job["expected_version"], job["binding_version"], report
                )
            completed = session.execute(
                text("""UPDATE federation.registration_jobs SET state='succeeded',report=CAST(:r AS jsonb),
              lease_token=NULL,lease_until=NULL WHERE tenant_id=:t AND id=:id AND lease_token=:token
                AND lease_until>clock_timestamp() RETURNING id"""),
                {"t": tenant, "id": identifier, "token": token, "r": json.dumps(report)},
            ).scalar_one_or_none()
            if completed is None:
                raise ValueError("Registration job lease expired before completion")
    except Exception:
        with TenantUnitOfWork(db, tenant) as session:
            session.execute(
                text("""UPDATE federation.registration_jobs SET state='failed',
              error_code=:code,lease_token=NULL,lease_until=NULL
              WHERE tenant_id=:t AND id=:id AND state='running' AND lease_token=:token
                AND lease_until>clock_timestamp()"""),
                {
                    "t": tenant,
                    "id": identifier,
                    "token": token,
                    "code": "ENDPOINT_CHECK_FAILED_OR_BINDING_CHANGED"
                    if job["kind"] == "endpoint_check"
                    else "QUALIFICATION_FAILED_OR_BINDING_CHANGED",
                },
            )
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings(_env_file=args.env_file)  # type: ignore[call-arg]
    if not settings.database_url or not settings.agent_onboarding_enabled:
        raise ValueError("Agent onboarding and database configuration required")
    db = create_runtime_engine(settings.database_url)
    network = (
        load_coordinator(settings)
        if settings.federation_coordinator_public_key is not None
        else load_runtime(settings)
    )
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
