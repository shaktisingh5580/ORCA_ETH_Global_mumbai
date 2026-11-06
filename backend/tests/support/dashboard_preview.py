"""Purpose: exercise the real dashboard and database APIs with test-only authentication/models.
Behavior: refuse non-loopback databases; seed labelled synthetic inputs and bind only localhost.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A05 browser gate; Phase: A05 test tooling only.
"""

import os
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import psycopg
import uvicorn
from live_database import prepare_database
from review_test_model import ReviewModel
from sqlalchemy import text

from orca.api.app import create_app
from orca.api.dependencies import current_principal
from orca.application.reasoning import ResearchReasoning
from orca.config import Settings
from orca.contracts.normalization import DatasetInput
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.reasoning_worker import execute
from orca.review_worker import execute as review_execute

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))
from test_reasoning_workflow import Model  # noqa: E402


def main():
    if "@127.0.0.1:55433/" not in os.environ.get("ORCA_TEST_DATABASE_URL", ""):
        raise RuntimeError("Preview requires isolated loopback test database")
    admin, runtime, tenant, _, project, _ = prepare_database()
    subject = str(uuid4())
    with psycopg.connect(admin) as c:
        c.execute(
            "INSERT INTO iam.memberships(id,tenant_id,subject,role) VALUES(%s,%s,%s,'org_owner')",
            (uuid4(), tenant, subject),
        )
        c.execute(
            "UPDATE iam.organizations SET name='ORCA Browser QA' WHERE tenant_id=%s", (tenant,)
        )
        c.execute(
            "UPDATE research.projects SET name='Synthetic observations' WHERE id=%s", (project,)
        )
    app = create_app(
        Settings(
            monitoring_token="0123456789abcdefghijklmnopqrstuvwxyz",
            database_url=runtime.replace("postgresql://", "postgresql+psycopg://", 1),
            object_store_endpoint="http://127.0.0.1:59000",
            object_store_access_key="test-only",
            object_store_secret_key="test-only",
            prototype_enabled=True,
            prototype_bundle_digest="sha256:" + "a" * 64,
            prototype_source_tenant=tenant,
        )
    )
    principal = Principal(subject, "browser-test", frozenset().union(*ROLE_SCOPES.values()))
    app.dependency_overrides[current_principal] = lambda: principal
    app.router.routes[:] = [
        r for r in app.router.routes if getattr(r, "path", None) != "/research/config"
    ]

    @app.get("/research/config")
    def config():
        return {
            "auth_url": "http://127.0.0.1:8099/test-auth",
            "anon_key": "test-only",
            "reasoning_configured": True,
            "arkiv_configured": False,
        }

    @app.post("/test-auth/token")
    def login():
        return {
            "access_token": "browser-test-only",
            "refresh_token": "browser-test-only",
            "expires_in": 3600,
            "user": {"email": "qa@example.test"},
        }

    db = app.state.runtime_engine
    with TenantUnitOfWork(db, tenant) as session:
        dataset = ResearchReasoning(session, tenant, project, principal).create(
            DatasetInput(
                name="Synthetic temperature control",
                format="csv",
                source_mode="synthetic_demo",
                content="time,temp\n2025-01-01T00:00:00Z,27\n2025-01-02T00:00:00Z,27\n",
            )
        )

    def work():
        while True:
            with db.connect() as c:
                plans = c.execute(text("SELECT * FROM iam.reasoning_pending_runs()")).all()
                reviews = c.execute(text("SELECT * FROM iam.pending_evidence_reviews()")).all()
            for t, r in plans:
                execute(db, t, r, Model(dataset.id))
            for t, r in reviews:
                review_execute(db, t, r, ReviewModel())
            time.sleep(1)

    threading.Thread(target=work, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=8099, log_level="warning")


if __name__ == "__main__":
    main()
