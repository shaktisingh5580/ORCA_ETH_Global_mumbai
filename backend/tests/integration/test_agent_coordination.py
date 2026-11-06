"""Purpose: exercise durable agent coordination over official A2A in separate real processes.
Behavior: use only synthetic local-test captures/DB, verify claims, exclusive assignment and review.
Refs: H04 gate and canonical protocol rules; Phase: H04; no cloud/chain writes in this test.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from live_database import prepare_database
from network_setup import configuration, source_recipe, start_provider
from sqlalchemy import text

from orca.api.app import create_app
from orca.api.dependencies import current_principal
from orca.application.prototype import digest_result
from orca.config import Settings
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.federation.signing import unb64
from orca.network_worker import process


@pytest.fixture
def network_http(tmp_path):
    admin, runtime_url, tenant, _, project, foreign = prepare_database()
    subject = str(uuid4())
    runtime, configfile, secrets = configuration(tmp_path)
    source, result = source_recipe(tmp_path)
    investigation, run = uuid4(), uuid4()
    with psycopg.connect(admin) as db:
        db.execute(
            "INSERT INTO iam.memberships(id,tenant_id,subject,role) VALUES(%s,%s,%s,'org_owner')",
            (uuid4(), tenant, subject),
        )
        db.execute(
            "INSERT INTO research.prototype_investigations(id,tenant_id,project_id,created_by,question,plan,state) VALUES(%s,%s,%s,%s,'Synthetic protocol test only','{}','approved')",
            (investigation, tenant, project, subject),
        )
        db.execute(
            "INSERT INTO research.prototype_runs(id,tenant_id,project_id,investigation_id,created_by,idempotency_key,bundle_digest,status,result,result_digest,report) VALUES(%s,%s,%s,%s,%s,'test',%s,'succeeded',%s::jsonb,%s,'test report')",
            (
                run,
                tenant,
                project,
                investigation,
                subject,
                result["bundle_digest"],
                json.dumps(result),
                digest_result(result),
            ),
        )
    app = create_app(
        Settings(
            monitoring_token="0123456789abcdefghijklmnopqrstuvwxyz",
            database_url=runtime_url.replace("postgresql://", "postgresql+psycopg://", 1),
            object_store_endpoint="http://127.0.0.1:59000",
            object_store_access_key="test-only",
            object_store_secret_key="test-only",
            prototype_enabled=True,
            prototype_bundle_digest=result["bundle_digest"],
            prototype_source_tenant=tenant,
            network_enabled=True,
            network_config_file=str(configfile),
            network_private_key=runtime.private_key,
        )
    )
    principal = Principal(subject, "test", frozenset().union(*ROLE_SCOPES.values()))
    app.dependency_overrides[current_principal] = lambda: principal
    with TenantUnitOfWork(app.state.runtime_engine, tenant) as session:
        for binding in runtime.configuration.agents:
            session.execute(
                text(
                    "INSERT INTO federation.agents(id,tenant_id,binding_version,binding) VALUES(:id,:t,:v,CAST(:b AS jsonb))"
                ),
                {
                    "id": binding.id,
                    "t": tenant,
                    "v": binding.binding_version,
                    "b": binding.model_dump_json(),
                },
            )
    children = []
    try:
        for binding, private in secrets:
            children.append(start_provider(tmp_path, configfile, binding, private, source))
        with TestClient(app) as client:
            yield client, tenant, project, foreign, run, principal
    finally:
        for child, log, _ in children:
            child.terminate()
            child.wait(timeout=15)
            log.close()
        app.state.runtime_engine.dispose()


def post(client, path, body, project=None, key=None):
    return client.post(
        path,
        json=body,
        params={"project_id": str(project)} if project else None,
        headers={"Idempotency-Key": key or str(uuid4())},
    )


def execute(client, tenant, operation):
    assert operation.status_code == 202, operation.text
    value = operation.json()
    assert process(
        client.app.state.runtime_engine,
        client.app.state.network_runtime,
        tenant,
        uuid_from(value["id"]),
    )
    result = client.get(
        f"/v1/network/operations/{value['id']}", params={"project_id": str(client.test_project)}
    )
    assert result.status_code == 200 and result.json()["status"] == "succeeded", result.text
    return value["task_id"]


def uuid_from(value):
    from uuid import UUID

    return UUID(value)


def begin_claimed(client, tenant, project, run):
    client.test_project = project
    task = execute(
        client,
        tenant,
        post(client, "/v1/network/tasks", {"project_id": str(project), "source_run_id": str(run)}),
    )
    ocean = client.app.state.network_runtime.configuration.agents[0]
    execute(
        client,
        tenant,
        post(client, f"/v1/network/tasks/{task}/claims", {"provider_id": str(ocean.id)}, project),
    )
    detail = client.get(f"/v1/network/tasks/{task}", params={"project_id": str(project)}).json()
    return task, detail["claims"][0]["id"]


@pytest.mark.live
@pytest.mark.enable_socket
def test_demo_http_is_project_scoped_and_correction_requires_a_challenge(network_http, tmp_path):
    from disagreement_demo import router

    client, tenant, project, foreign, run, _ = network_http
    client.app.include_router(router)
    client.app.state.disagreement_state = str(tmp_path / "isolated-demo")
    body = {"project_id": str(project)}
    key = str(uuid4())
    created = post(client, "/v1/demo/disagreements", body, key=key)
    assert created.status_code == 202, created.text
    assert post(client, "/v1/demo/disagreements", body, key=key).json() == created.json()
    identifier = created.json()["id"]
    assert created.json()["source_mode"] == "synthetic_demo"
    assert created.json()["stages"] == []
    assert (
        client.get(
            f"/v1/demo/disagreements/{identifier}", params={"project_id": str(foreign)}
        ).status_code
        == 404
    )
    assert (
        post(
            client,
            f"/v1/demo/disagreements/{identifier}/correct",
            {"expected_version": 1},
            project,
        ).status_code
        == 409
    )
    task, _ = begin_claimed(client, tenant, project, run)
    denied = post(
        client,
        "/v1/network/tasks",
        {
            "project_id": str(project),
            "source_run_id": str(run),
            "correction_of": task,
        },
    )
    assert denied.status_code == 409


@pytest.mark.live
@pytest.mark.enable_socket
def test_real_a2a_reproduction_then_separately_keyed_review_with_durable_artifacts(network_http):
    client, tenant, project, foreign, run, _ = network_http
    task, claim = begin_claimed(client, tenant, project, run)
    assert (
        client.get(f"/v1/network/tasks/{task}", params={"project_id": str(foreign)}).status_code
        == 404
    )
    key = str(uuid4())
    body = {"claim_id": claim, "expected_generation": 0}
    submitted = post(client, f"/v1/network/tasks/{task}/assignments", body, project, key)
    assert (
        post(client, f"/v1/network/tasks/{task}/assignments", body, project, key).json()["id"]
        == submitted.json()["id"]
    )
    assert (
        post(
            client,
            f"/v1/network/tasks/{task}/assignments",
            {**body, "expected_generation": 1},
            project,
            key,
        ).status_code
        == 409
    )
    execute(client, tenant, submitted)
    detail = client.get(f"/v1/network/tasks/{task}", params={"project_id": str(project)}).json()
    assert (
        detail["state"] == "completed"
        and len(detail["assignments"]) == len(detail["artifacts"]) == 1
    )
    assert detail["assignments"][0]["a2a_task_id"] and detail["assignments"][0]["a2a_context_id"]
    configured = client.app.state.network_runtime
    ocean = configured.configuration.agents[0]
    now = int(time.time())
    claims = {
        "iss": str(configured.configuration.coordinator.id),
        "aud": str(ocean.id),
        "iat": now,
        "exp": now + 60,
        "jti": submitted.json()["id"],
        "task_id": task,
        "scope": "execute",
        "request_digest": "read-scope",
    }

    def bearer(payload):
        return jwt.encode(
            payload,
            Ed25519PrivateKey.from_private_bytes(unb64(configured.private_key)),
            algorithm="EdDSA",
            headers={"kid": configured.configuration.coordinator.key_id},
        )

    request = {
        "jsonrpc": "2.0",
        "id": "read",
        "method": "tasks/get",
        "params": {"id": detail["assignments"][0]["a2a_task_id"]},
    }
    with httpx.Client(trust_env=False) as http:
        fetched = http.post(
            ocean.endpoint, json=request, headers={"Authorization": "Bearer " + bearer(claims)}
        )
        assert (
            fetched.status_code == 200
            and fetched.json()["result"]["status"]["state"] == "completed"
        )
        denied = http.post(
            ocean.endpoint,
            json=request,
            headers={"Authorization": "Bearer " + bearer({**claims, "jti": str(uuid4())})},
        )
        assert "error" in denied.json()
        expired = http.post(
            ocean.endpoint,
            json=request,
            headers={
                "Authorization": "Bearer " + bearer({**claims, "iat": now - 120, "exp": now - 60})
            },
        )
        assert expired.status_code == 401
    evidence = detail["artifacts"][0]["envelope"]["payload"]["results"]
    assert evidence["verdict"] == "reproduced" and evidence["anomaly"] == pytest.approx(1)
    assert evidence["raw_source_digests_verified"] == 21
    reviewer = client.app.state.network_runtime.configuration.agents[1]
    review = execute(
        client,
        tenant,
        post(
            client,
            "/v1/network/tasks",
            {
                "project_id": str(project),
                "source_run_id": str(run),
                "capability": "sst_review",
                "review_of": task,
            },
        ),
    )
    execute(
        client,
        tenant,
        post(
            client, f"/v1/network/tasks/{review}/claims", {"provider_id": str(reviewer.id)}, project
        ),
    )
    review_detail = client.get(
        f"/v1/network/tasks/{review}", params={"project_id": str(project)}
    ).json()
    execute(
        client,
        tenant,
        post(
            client,
            f"/v1/network/tasks/{review}/assignments",
            {"claim_id": review_detail["claims"][0]["id"], "expected_generation": 0},
            project,
        ),
    )
    final = client.get(f"/v1/network/tasks/{review}", params={"project_id": str(project)}).json()
    reviewed = final["artifacts"][0]["envelope"]["payload"]
    assert reviewed["review_of_digest"] == detail["artifacts"][0]["envelope"]["content_digest"]
    assert (
        reviewed["results"]["implementation"]
        == "separate-year-first-regional-reduction/hackathon-v1"
    )
    assert reviewed["results"]["verdict"] == "reproduced"
    timeline = client.get(
        f"/v1/network/tasks/{review}/timeline", params={"project_id": str(project)}
    ).json()
    assert {event["kind"] for event in timeline["events"]} == {
        "task",
        "claim",
        "assignment",
        "review",
    }
    assert timeline["live_chain"] is None and all(
        event["receipt"] is None for event in timeline["events"]
    )
    with httpx.Client(trust_env=False) as http:
        denied = http.post(
            reviewer.endpoint,
            json={
                "jsonrpc": "2.0",
                "id": "x",
                "method": "tasks/get",
                "params": {"id": "not-yours"},
            },
        )
    assert denied.status_code == 401


@pytest.mark.live
@pytest.mark.enable_socket
def test_two_assignment_requests_choose_one_winner_and_revocation_blocks_dispatch(network_http):
    client, tenant, project, _, run, _ = network_http
    task, claim = begin_claimed(client, tenant, project, run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                post,
                client,
                f"/v1/network/tasks/{task}/assignments",
                {"claim_id": claim, "expected_generation": 0},
                project,
            )
            for _ in range(2)
        ]
        replies = [future.result() for future in futures]
    assert sorted(response.status_code for response in replies) == [202, 409]
    selected = next(response for response in replies if response.status_code == 202)
    with TenantUnitOfWork(client.app.state.runtime_engine, tenant) as session:
        session.execute(
            text("UPDATE federation.agents SET revoked=true WHERE tenant_id=:t AND id=:id"),
            {"t": tenant, "id": client.app.state.network_runtime.configuration.agents[0].id},
        )
    assert process(
        client.app.state.runtime_engine,
        client.app.state.network_runtime,
        tenant,
        uuid_from(selected.json()["id"]),
    )
    detail = client.get(f"/v1/network/tasks/{task}", params={"project_id": str(project)}).json()
    assert detail["artifacts"] == [] and detail["operations"][-1]["status"] == "failed"


@pytest.mark.live
@pytest.mark.enable_socket
def test_cancellation_during_actual_remote_execution_rejects_late_artifact(
    network_http, monkeypatch
):
    from orca import network_worker

    client, tenant, project, _, run, _ = network_http
    task, claim = begin_claimed(client, tenant, project, run)
    submitted = post(
        client,
        f"/v1/network/tasks/{task}/assignments",
        {"claim_id": claim, "expected_generation": 0},
        project,
    )
    original = network_worker.exchange

    def late(*args):
        response = original(*args)
        assert (
            post(
                client, f"/v1/network/tasks/{task}/cancel", {"expected_generation": 1}, project
            ).status_code
            == 202
        )
        return response

    monkeypatch.setattr(network_worker, "exchange", late)
    process(
        client.app.state.runtime_engine,
        client.app.state.network_runtime,
        tenant,
        uuid_from(submitted.json()["id"]),
    )
    detail = client.get(f"/v1/network/tasks/{task}", params={"project_id": str(project)}).json()
    assert detail["state"] == "cancelled" and detail["artifacts"] == []


@pytest.mark.live
@pytest.mark.enable_socket
def test_lost_a2a_response_retries_same_operation_and_provider_replays_durable_output(
    network_http, monkeypatch
):
    from orca import network_worker

    client, tenant, project, _, run, _ = network_http
    task, claim = begin_claimed(client, tenant, project, run)
    submitted = post(
        client,
        f"/v1/network/tasks/{task}/assignments",
        {"claim_id": claim, "expected_generation": 0},
        project,
    )
    original = network_worker.exchange
    seen = []

    def lost(*args):
        result = original(*args)
        seen.append(result["envelope"])
        if len(seen) == 1:
            raise httpx.ReadTimeout("Synthetic lost response after the real provider finished")
        return result

    monkeypatch.setattr(network_worker, "exchange", lost)
    process(
        client.app.state.runtime_engine,
        client.app.state.network_runtime,
        tenant,
        uuid_from(submitted.json()["id"]),
    )
    status = client.get(
        f"/v1/network/operations/{submitted.json()['id']}", params={"project_id": str(project)}
    ).json()
    assert status["status"] == "queued"
    execute(client, tenant, submitted)
    assert len(seen) == 2 and seen[0] == seen[1]
    detail = client.get(f"/v1/network/tasks/{task}", params={"project_id": str(project)}).json()
    assert detail["state"] == "completed" and len(detail["artifacts"]) == 1


@pytest.mark.live
@pytest.mark.enable_socket
def test_uncertain_chain_submission_holds_for_reconciliation_without_resend(network_http):
    from dataclasses import replace

    from orca.infrastructure.federation.runtime import ChainUnavailable

    client, tenant, project, _, run, _ = network_http
    seen = []

    class UncertainGateway:
        def publish(self, *args):
            seen.append(args)
            raise ChainUnavailable(True)

    runtime = replace(client.app.state.network_runtime, gateway=UncertainGateway())
    created = post(
        client,
        "/v1/network/tasks",
        {"project_id": str(project), "source_run_id": str(run), "approve_public_reference": True},
    )
    identifier = uuid_from(created.json()["id"])
    assert process(client.app.state.runtime_engine, runtime, tenant, identifier)
    assert not process(client.app.state.runtime_engine, runtime, tenant, identifier)
    result = client.get(
        f"/v1/network/operations/{identifier}", params={"project_id": str(project)}
    ).json()
    assert result["status"] == "reconciliation_required" and len(seen) == 1
