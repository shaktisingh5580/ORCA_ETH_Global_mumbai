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
from orca.application.review import EvidenceReview
from orca.contracts.normalization import DatasetInput
from orca.contracts.reasoning import ReasoningRequest
from orca.contracts.review import CandidateResult
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.reasoning_worker import execute
from orca.review_worker import execute as review_execute


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
def test_durable_reasoning_approval_and_immutable_retry(tmp_path, monkeypatch):
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
        from review_test_model import ReviewModel

        with TenantUnitOfWork(db, tenant) as s:
            app = EvidenceReview(s, tenant, project, principal)
            reference = app.reference(run["id"])
            a = reference["artifacts"][0]
            candidate = CandidateResult(
                label="Synthetic injected discrepancy",
                source_mode="synthetic_demo",
                plan_digest=plan_digest,
                means_celsius={a["dataset_id"]: 28},
                evidence_digests={a["dataset_id"]: a["artifact_digest"]},
            )
            review = app.submit_review(run["id"], candidate)
        assert review_execute(db, tenant, review["id"], ReviewModel())
        with TenantUnitOfWork(db, tenant) as s:
            app = EvidenceReview(s, tenant, project, principal)
            original = app.review_row(review["id"])
            assert (
                original["state"] == "completed"
                and original["decision"]["reviewer"]["verdict"] == "challenged"
            )
            corrected = app.correct(review["id"], review["candidate_digest"])
            assert corrected["parent_review_id"] == review["id"]
            assert app.correct(review["id"], review["candidate_digest"])["id"] == corrected["id"]
        assert review_execute(db, tenant, corrected["id"], ReviewModel())
        with TenantUnitOfWork(db, tenant) as s:
            app = EvidenceReview(s, tenant, project, principal)
            assert app.review_row(corrected["id"])["decision"]["reviewer"]["verdict"] == "agrees"
            assert app.review_row(review["id"])["candidate"]["means_celsius"][a["dataset_id"]] == 28
        from network_setup import configuration

        from orca.anchor_worker import execute as publish_anchor
        from orca.application.anchors import ResearchAnchors
        from orca.infrastructure.federation.runtime import ArkivGateway
        from orca.infrastructure.federation.signing import verify

        network, _, _ = configuration(tmp_path)
        published = []

        def fake_publish(self, envelope, binding):
            verify(envelope, binding.public_key, binding.key_id, "public")
            assert envelope.payload["kind"] == "evidence"
            assert envelope.payload["source_mode"] == "synthetic_demo"
            published.append(envelope)
            return {"entity_key": "0x" + "a" * 64, "tx_hash": "0x" + "b" * 64}

        monkeypatch.setattr(ArkivGateway, "publish", fake_publish)
        with TenantUnitOfWork(db, tenant) as s:
            app = ResearchAnchors(s, tenant, project, principal)
            anchor = app.publish(run["id"])
            assert app.publish(run["id"])["id"] == anchor["id"]
            assert len(anchor["manifest"]["reviews"]) == 2
        assert publish_anchor(db, network, tenant, anchor["id"])
        assert not publish_anchor(db, network, tenant, anchor["id"])
        assert len(published) == 1
        with TenantUnitOfWork(db, tenant) as s:
            assert (
                ResearchAnchors(s, tenant, project, principal).anchors(run["id"])[0]["state"]
                == "indexed"
            )
        with TenantUnitOfWork(db, other) as s:
            assert (
                s.execute(text("SELECT count(*) FROM research.evidence_anchors")).scalar_one() == 0
            )
    finally:
        db.dispose()
