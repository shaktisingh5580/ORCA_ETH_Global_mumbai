"""Purpose: run bounded Gemini planning with durable events and fenced recovery.
Behavior: recheck project access at every persisted event and never execute unapproved writes.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A02; existing prototype worker; Phase: A02.
"""

import argparse
import time
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from orca.application.reasoning import ResearchReasoning
from orca.application.reasoning_loop import Reasoner, plan_research
from orca.config import Settings
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.domain.normalization import digest
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.gemini_reasoning import GeminiReasoner, ModelUnavailable


def execute(db: Engine, tenant: UUID, identifier: UUID, model: Reasoner) -> bool:
    token = uuid4()
    with TenantUnitOfWork(db, tenant) as session:
        row = (
            session.execute(
                text("""SELECT * FROM research.reasoning_runs WHERE tenant_id=:t AND id=:id
          AND (state='queued' OR (state='planning' AND lease_until<clock_timestamp())) FOR UPDATE SKIP LOCKED"""),
                {"t": tenant, "id": identifier},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return False
        if row["attempts"] >= 2:
            session.execute(
                text(
                    "UPDATE research.reasoning_runs SET state='failed',error_code='RETRY_LIMIT' WHERE tenant_id=:t AND id=:id"
                ),
                {"t": tenant, "id": identifier},
            )
            return True
        job = dict(row)
        session.execute(
            text("""UPDATE research.reasoning_runs SET state='planning',attempts=attempts+1,
          lease_token=:token,lease_until=clock_timestamp()+interval '240 seconds',updated_at=now()
          WHERE tenant_id=:t AND id=:id"""),
            {"t": tenant, "id": identifier, "token": token},
        )
    principal = Principal(
        job["created_by"], "trusted-reasoning-worker", frozenset().union(*ROLE_SCOPES.values())
    )

    def emit(kind: str, data: dict[str, Any]) -> None:
        with TenantUnitOfWork(db, tenant) as session:
            app = ResearchReasoning(session, tenant, job["project_id"], principal)
            app.authorize("project:write")
            owned = session.execute(
                text("""SELECT id FROM research.reasoning_runs WHERE tenant_id=:t AND id=:id
              AND state='planning' AND lease_token=:token AND lease_until>clock_timestamp() FOR UPDATE"""),
                {"t": tenant, "id": identifier, "token": token},
            ).scalar_one_or_none()
            if owned is None:
                raise ValueError("WORKER_LEASE_LOST")
            app.event(identifier, kind, data)

    try:
        with TenantUnitOfWork(db, tenant) as session:
            app = ResearchReasoning(session, tenant, job["project_id"], principal)
            app.authorize("project:write")
            datasets = {
                value: dict(app.row(UUID(value))) for value in job["request"]["dataset_ids"]
            }
        emit("planning_started", {"attempt": job["attempts"] + 1})
        plan = plan_research(job["request"]["question"], datasets, model, emit)
        with TenantUnitOfWork(db, tenant) as session:
            ResearchReasoning(session, tenant, job["project_id"], principal).authorize(
                "project:write"
            )
            session.execute(
                text("""UPDATE research.reasoning_runs SET state=:state,plan=CAST(:plan AS jsonb),
              plan_digest=:digest,updated_at=now(),lease_until=NULL WHERE tenant_id=:t AND id=:id
              AND state='planning' AND lease_token=:token AND lease_until>clock_timestamp()"""),
                {
                    "t": tenant,
                    "id": identifier,
                    "token": token,
                    "state": "planned" if plan.outcome == "supported" else "blocked",
                    "plan": plan.model_dump_json(),
                    "digest": digest(plan.model_dump(mode="json")),
                },
            )
    except Exception as error:
        code = str(error) if isinstance(error, ModelUnavailable) else "REASONING_FAILED"
        with TenantUnitOfWork(db, tenant) as session:
            session.execute(
                text("""UPDATE research.reasoning_runs SET state='failed',error_code=:code,updated_at=now()
              WHERE tenant_id=:t AND id=:id AND state='planning' AND lease_token=:token"""),
                {"t": tenant, "id": identifier, "token": token, "code": code[:100]},
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
    key = settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else ""
    while True:
        if key:
            model = GeminiReasoner(key, settings.gemini_model or "gemini-2.5-flash")
            with db.connect() as connection:
                jobs = connection.execute(text("SELECT * FROM iam.reasoning_pending_runs()")).all()
            for tenant, identifier in jobs:
                execute(db, UUID(str(tenant)), UUID(str(identifier)), model)
        if args.once:
            break
        time.sleep(2)
    db.dispose()


if __name__ == "__main__":
    main()
