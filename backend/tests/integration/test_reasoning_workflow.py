"""Purpose: verify durable model planning, approval and event ownership with real Postgres.
Behavior: test-only model calls exercise the real gateway and worker against isolated storage.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A02; Phase: A02.
"""

from uuid import uuid4

import psycopg
import pytest
from live_database import prepare_database
from sqlalchemy import text

from orca.application.reasoning import ResearchReasoning
from orca.contracts.normalization import DatasetInput
from orca.contracts.reasoning import ReasoningRequest
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.reasoning_worker import execute


class Model:
    def __init__(self, identifier):
        mapping = {"timestamp_column": "time", "value_column": "temp", "unit": "C"}
        self.calls = iter(
            [
                ("inspect_dataset", {"dataset_id": str(identifier)}),
                ("preview_mapping", {"dataset_id": str(identifier), "mapping": mapping}),
                (
                    "submit_plan",
                    {
                        "outcome": "supported",
                        "recipe": "normalize",
                        "summary": "Normalize Celsius observations",
                        "datasets": [
                            {
                                "dataset_id": str(identifier),
                                "role": "representation",
                                "mapping": mapping,
                            }
                        ],
                        "missing_evidence": [],
                        "limitations": ["Synthetic data, no independent review"],
                    },
                ),
            ]
        )

    def turn(self, instruction, contents, tools):
        name, args = next(self.calls)
        return {
            "model": "test-only",
            "content": {"role": "model", "parts": [{"functionCall": {"name": name, "args": args}}]},
        }


@pytest.mark.live
@pytest.mark.enable_socket
def test_durable_reasoning_approval_and_immutable_retry():
    admin, runtime, tenant, other, project, foreign = prepare_database()
    subject = str(uuid4())
    with psycopg.connect(admin) as c:
        c.execute(
            "INSERT INTO iam.memberships(id,tenant_id,subject,role) VALUES(%s,%s,%s,'org_owner')",
            (uuid4(), tenant, subject),
        )
    db = create_runtime_engine(runtime.replace("postgresql://", "postgresql+psycopg://", 1))
    principal = Principal(subject, "test", frozenset().union(*ROLE_SCOPES.values()))
    try:
        with TenantUnitOfWork(db, tenant) as s:
            app = ResearchReasoning(s, tenant, project, principal)
            data = app.create(
                DatasetInput(
                    name="Synthetic",
                    format="csv",
                    content="time,temp\n2025-01-01T00:00:00Z,27\n",
                    source_mode="synthetic_demo",
                )
            )
            body = ReasoningRequest(
                question="Normalize these Celsius observations", dataset_ids=[data.id]
            )
            run = app.create_run(body, "request-1")
            assert app.create_run(body, "request-1")["id"] == run["id"]
        assert execute(db, tenant, run["id"], Model(data.id))
        assert not execute(db, tenant, run["id"], Model(data.id))
        with TenantUnitOfWork(db, tenant) as s:
            app = ResearchReasoning(s, tenant, project, principal)
            saved = app.run_row(run["id"])
            assert saved["state"] == "planned"
            assert len(app.events(run["id"])) >= 7
            plan_digest = saved["plan_digest"]
        with pytest.raises(ValueError), TenantUnitOfWork(db, tenant) as s:
            ResearchReasoning(s, tenant, project, principal).approve_plan(run["id"], "stale")
        with TenantUnitOfWork(db, tenant) as s:
            app = ResearchReasoning(s, tenant, project, principal)
            result = app.approve_plan(run["id"], plan_digest)
            assert result["state"] == "completed"
            assert result["result"]["artifacts"][0]["summary"]["mean_celsius"] == 27
            assert app.approve_plan(run["id"], plan_digest) == result
        with TenantUnitOfWork(db, other) as s:
            assert (
                s.execute(text("SELECT count(*) FROM research.reasoning_events")).scalar_one() == 0
            )
    finally:
        db.dispose()
