"""Purpose: execute evidence reviews with model explanations and deterministic verification.
Behavior: lease queued reviews, recheck project authority and append actual reviewer/coordinator events.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A03; Phase: A03.
"""

import argparse
import json
import time
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from orca.application.reasoning_loop import Reasoner
from orca.application.review import EvidenceReview
from orca.application.review_loop import reason_about_review
from orca.config import Settings
from orca.contracts.review import CandidateResult
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.domain.review import compare_candidate
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.gemini_reasoning import GeminiReasoner, ModelUnavailable


def execute(db: Engine, tenant: UUID, identifier: UUID, model: Reasoner) -> bool:
    token = uuid4()
    with TenantUnitOfWork(db, tenant) as session:
        row = (
            session.execute(
                text("""SELECT * FROM research.evidence_reviews WHERE tenant_id=:t AND id=:id
          AND (state='queued' OR (state='reviewing' AND lease_until<clock_timestamp())) FOR UPDATE SKIP LOCKED"""),
                {"t": tenant, "id": identifier},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return False
        job = dict(row)
        if job["attempts"] >= 2:
            session.execute(
                text(
                    "UPDATE research.evidence_reviews SET state='failed',error_code='RETRY_LIMIT' WHERE tenant_id=:t AND id=:id"
                ),
                {"t": tenant, "id": identifier},
            )
            return True
        session.execute(
            text("""UPDATE research.evidence_reviews SET state='reviewing',lease_token=:token,
          lease_until=clock_timestamp()+interval '420 seconds',attempts=attempts+1 WHERE tenant_id=:t AND id=:id"""),
            {"t": tenant, "id": identifier, "token": token},
        )
    principal = Principal(
        job["created_by"], "trusted-review-worker", frozenset().union(*ROLE_SCOPES.values())
    )

    def emit(kind: str, data: dict[str, Any]) -> None:
        with TenantUnitOfWork(db, tenant) as session:
            app = EvidenceReview(session, tenant, job["project_id"], principal)
            app.authorize("project:write")
            owned = session.execute(
                text("""SELECT id FROM research.evidence_reviews WHERE tenant_id=:t AND id=:id
              AND state='reviewing' AND lease_token=:token AND lease_until>clock_timestamp() FOR UPDATE"""),
                {"t": tenant, "id": identifier, "token": token},
            ).scalar_one_or_none()
            if owned is None:
                raise ValueError("REVIEW_LEASE_LOST")
            app.event(job["run_id"], kind, {"review_id": str(identifier), **data})

    try:
        with TenantUnitOfWork(db, tenant) as session:
            app = EvidenceReview(session, tenant, job["project_id"], principal)
            app.authorize("project:write")
            reference = app.reference(job["run_id"])
        candidate = CandidateResult.model_validate(job["candidate"])
        findings = compare_candidate(candidate, reference["plan_digest"], reference["artifacts"])
        packet = {
            "candidate": candidate.model_dump(mode="json"),
            "approved_reference": reference,
            "findings": findings,
        }
        emit("review_comparison", packet)
        decision = reason_about_review(packet, model, emit)
        with TenantUnitOfWork(db, tenant) as session:
            EvidenceReview(session, tenant, job["project_id"], principal).authorize("project:write")
            session.execute(
                text("""UPDATE research.evidence_reviews SET state='completed',findings=CAST(:findings AS jsonb),
              decision=CAST(:decision AS jsonb) WHERE tenant_id=:t AND id=:id AND state='reviewing'
              AND lease_token=:token AND lease_until>clock_timestamp()"""),
                {
                    "t": tenant,
                    "id": identifier,
                    "token": token,
                    "findings": json.dumps(findings),
                    "decision": json.dumps(decision),
                },
            )
    except Exception as error:
        code = str(error) if isinstance(error, ModelUnavailable) else "REVIEW_FAILED"
        with TenantUnitOfWork(db, tenant) as session:
            session.execute(
                text("""UPDATE research.evidence_reviews SET state='failed',error_code=:code
              WHERE tenant_id=:t AND id=:id AND state='reviewing' AND lease_token=:token"""),
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
                jobs = connection.execute(
                    text("SELECT * FROM iam.pending_evidence_reviews()")
                ).all()
            for tenant, identifier in jobs:
                execute(db, UUID(str(tenant)), UUID(str(identifier)), model)
        if args.once:
            break
        time.sleep(2)
    db.dispose()


if __name__ == "__main__":
    main()
