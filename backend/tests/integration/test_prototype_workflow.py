"""Purpose: verify the hackathon approval/run flow using actual isolated tenant persistence.
Behavior: test HTTP idempotency, approval, token/project denial and completed-result immutability.
Refs: HACKATHON_IMPLEMENTATION_PLAN.md H02/H03; Phase: H02/H03; synthetic data confined to test DB.
"""

from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from live_database import prepare_database
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from orca.api.app import create_app
from orca.api.dependencies import current_principal
from orca.application.prototype import digest_result
from orca.config import Settings
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork


@pytest.fixture
def prototype_http():
    admin, runtime, tenant, other, project, foreign = prepare_database()
    subject = str(uuid4())
    with psycopg.connect(admin) as connection:
        connection.execute(
            "INSERT INTO iam.memberships(id,tenant_id,subject,role) VALUES(%s,%s,%s,'org_owner')",
            (uuid4(), tenant, subject),
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
    principal = Principal(subject, "test", frozenset().union(*ROLE_SCOPES.values()))
    app.dependency_overrides[current_principal] = lambda: principal
    with TestClient(app) as client:
        yield client, tenant, project, foreign, principal
    app.state.runtime_engine.dispose()


@pytest.mark.live
@pytest.mark.enable_socket
def test_prototype_requires_approval_and_preserves_idempotency_and_project_boundaries(
    prototype_http,
):
    client, _, project, foreign, principal = prototype_http
    created = client.post(
        "/v1/prototype/investigations",
        json={
            "project_id": str(project),
            "question": "How did January sea surface temperature compare with its reference?",
        },
    )
    assert created.status_code == 201, created.text
    investigation = created.json()
    detail_url = f"/v1/prototype/investigations/{investigation['id']}"
    assert client.get(detail_url, params={"project_id": str(project)}).json() == investigation
    assert client.get(detail_url, params={"project_id": str(foreign)}).status_code == 404
    assert (
        client.get(
            f"/v1/prototype/investigations/{uuid4()}", params={"project_id": str(project)}
        ).status_code
        == 404
    )
    assert (
        investigation["plan"]["planner"] == "guided"
        and investigation["plan"]["source_mode"] == "real"
    )
    body = {"project_id": str(project), "investigation_id": investigation["id"]}
    headers = {"Idempotency-Key": "run-once"}
    assert client.post("/v1/prototype/runs", json=body, headers=headers).status_code == 409
    assert (
        client.post(
            f"/v1/prototype/investigations/{investigation['id']}/approve",
            params={"project_id": str(project)},
            json={"approve": True},
        ).status_code
        == 200
    )
    first = client.post("/v1/prototype/runs", json=body, headers=headers)
    second = client.post("/v1/prototype/runs", json=body, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"] and first.json()["status"] == "queued"
    run = first.json()["id"]
    filtered = client.get(
        "/v1/prototype/runs",
        params={"project_id": str(project), "investigation_id": investigation["id"]},
    )
    assert filtered.status_code == 200 and [row["id"] for row in filtered.json()] == [run]
    assert (
        client.get(
            "/v1/prototype/runs",
            params={"project_id": str(project), "investigation_id": str(uuid4())},
        ).json()
        == []
    )
    assert (
        client.get(f"/v1/prototype/runs/{run}", params={"project_id": str(foreign)}).status_code
        == 404
    )
    assert (
        client.get(
            f"/v1/prototype/runs/{run}/report", params={"project_id": str(project)}
        ).status_code
        == 409
    )
    client.app.dependency_overrides[current_principal] = lambda: Principal(
        principal.subject, "test", frozenset({"project:read"})
    )
    assert (
        client.post(
            "/v1/prototype/investigations",
            json={
                "project_id": str(project),
                "question": "This token cannot create investigations",
            },
        ).status_code
        == 403
    )
    client.app.dependency_overrides.clear()
    # Exercise missing-token rejection with authentication configured; verification is never called.
    client.app.state.oidc_verifier = object()
    assert client.get(detail_url, params={"project_id": str(project)}).status_code == 401


@pytest.mark.live
@pytest.mark.enable_socket
def test_prototype_completed_result_is_immutable_and_report_is_private(prototype_http):
    client, tenant, project, foreign, _ = prototype_http
    investigation = client.post(
        "/v1/prototype/investigations",
        json={
            "project_id": str(project),
            "question": "Compare the registered January temperatures",
        },
    ).json()
    client.post(
        f"/v1/prototype/investigations/{investigation['id']}/approve",
        params={"project_id": str(project)},
        json={"approve": True},
    )
    run = client.post(
        "/v1/prototype/runs",
        json={"project_id": str(project), "investigation_id": investigation["id"]},
        headers={"Idempotency-Key": "result"},
    ).json()
    import json

    result = {"source_mode": "test-only", "anomaly": 1.0}
    with TenantUnitOfWork(client.app.state.runtime_engine, tenant) as session:
        session.execute(
            text(
                "UPDATE research.prototype_runs SET status='succeeded',result=CAST(:result AS jsonb),result_digest=:digest,report='test-only report' WHERE tenant_id=:t AND id=:id"
            ),
            {
                "t": tenant,
                "id": run["id"],
                "result": json.dumps(result),
                "digest": digest_result(result),
            },
        )
    assert (
        client.get(f"/v1/prototype/runs/{run['id']}", params={"project_id": str(project)}).json()[
            "result"
        ]
        == result
    )
    assert (
        client.get(
            f"/v1/prototype/runs/{run['id']}/report", params={"project_id": str(project)}
        ).text
        == "test-only report"
    )
    assert (
        client.get(
            f"/v1/prototype/runs/{run['id']}/report", params={"project_id": str(foreign)}
        ).status_code
        == 404
    )
    with pytest.raises(DBAPIError):
        with TenantUnitOfWork(client.app.state.runtime_engine, tenant) as session:
            session.execute(
                text(
                    "UPDATE research.prototype_runs SET report='changed' WHERE tenant_id=:t AND id=:id"
                ),
                {"t": tenant, "id": run["id"]},
            )
