"""Purpose: exercise durable agent coordination over official A2A in separate real processes.
Behavior: use only synthetic local-test captures/DB, verify claims, exclusive assignment and review.
Refs: H04 gate, main_implementation plan.md P11.2 and api design.md §5.2. Phase: H04 and P11 regression.
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
def network_http(tmp_path, request):
    admin, runtime_url, tenant, _, project, foreign = prepare_database()
    subject = str(uuid4())
    runtime, configfile, secrets = configuration(
        tmp_path, two_claimants=getattr(request, "param", False)
    )
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
            client.test_provider_keys = {binding.id: private for binding, private in secrets}
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
@pytest.mark.parametrize("network_http", [True], indirect=True)
def test_two_claimants_one_explicit_assignment(network_http):
    client, tenant, project, _, run, _ = network_http
    task, claim = begin_claimed(client, tenant, project, run)
    other = client.app.state.network_runtime.configuration.agents[1]
    execute(
        client,
        tenant,
        post(client, f"/v1/network/tasks/{task}/claims", {"provider_id": str(other.id)}, project),
    )
    details = client.get(f"/v1/network/tasks/{task}", params={"project_id": str(project)}).json()
    competing_claims = {item["provider_id"]: item["id"] for item in details["claims"]}
    assert len(competing_claims) == 2
    assert competing_claims[str(other.id)] != claim
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                post,
                client,
                f"/v1/network/tasks/{task}/assignments",
                {"claim_id": candidate, "expected_generation": 0},
                project,
            )
            for candidate in competing_claims.values()
        ]
        replies = [future.result() for future in futures]
    assert sorted(response.status_code for response in replies) == [202, 409]
    selected = next(response for response in replies if response.status_code == 202)
    detail = client.get(f"/v1/network/tasks/{task}", params={"project_id": str(project)}).json()
    assert len(detail["assignments"]) == 1
    winner = detail["assignments"][0]["provider_id"]
    with TenantUnitOfWork(client.app.state.runtime_engine, tenant) as session:
        session.execute(
            text("UPDATE federation.agents SET revoked=true WHERE tenant_id=:t AND id=:id"),
            {"t": tenant, "id": uuid_from(winner)},
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


@pytest.mark.live
@pytest.mark.enable_socket
def test_arkiv_v1_task_claim_and_assignment_use_authenticated_worker(network_http, monkeypatch):
    from dataclasses import replace

    from orca import network_worker
    from orca.infrastructure.federation.public_events import public_claim_attributes, public_event

    client, tenant, project, _, run, principal = network_http
    runtime = client.app.state.network_runtime
    coordinator = runtime.configuration.coordinator
    provider = runtime.configuration.agents[0]
    task_key, claim_key, assignment_key = ("0x" + character * 64 for character in "abc")
    legacy_claim_key = "0x" + "d" * 64
    legacy_records = {}
    intents = []
    v1_records = {}

    class Chain:
        def publish(self, envelope, writer):
            key = "0x" + envelope.content_digest.removeprefix("sha256:")
            legacy_records[key] = self.record(key, envelope, writer)
            return {"entity_key": key}

        def record(self, key, envelope, writer):
            return {
                "entity_key": key,
                "owner": writer.wallet,
                "public_key": writer.public_key,
                "envelope": envelope.model_dump(mode="json"),
            }

        def call(self, payload):
            if payload["command"] == "query":
                return {"truncated": False, "records": list(legacy_records.values())}
            return legacy_records[payload["entity_key"]]

        def read(self, key, writer, envelope):
            from orca.infrastructure.federation.runtime import verify_chain_record

            verify_chain_record(legacy_records[key], writer, envelope)

        def public_v1_publish(self, intent):
            intents.append(intent)
            return {
                "status": "indexed",
                "entity_key": task_key if intent["kind"] == "task" else assignment_key,
            }

        def public_v1_read(self, kind, key, owner):
            assert kind == "claim" and key == claim_key and owner == provider.wallet
            return v1_records[key]

    chain = Chain()
    runtime = replace(runtime, gateway=chain)
    task_body = {
        "project_id": str(project),
        "source_run_id": str(run),
        "publish_arkiv_v1": True,
    }
    assert post(client, "/v1/network/tasks", task_body).status_code == 409
    created = post(client, "/v1/network/tasks", {**task_body, "approve_public_reference": True})
    assert created.status_code == 202, created.text
    task_id = created.json()["task_id"]

    def handle(operation, expected):
        assert operation.status_code == 202, operation.text
        identifier = uuid_from(operation.json()["id"])
        assert process(client.app.state.runtime_engine, runtime, tenant, identifier)
        status = client.get(
            f"/v1/network/operations/{identifier}", params={"project_id": str(project)}
        )
        assert status.json()["status"] == expected, status.text

    handle(created, "succeeded")
    assert intents[0]["owner"] == coordinator.wallet
    assert intents[0]["attributes"]["regionCode"] == "global"
    assert set(intents[0]["attributes"]) == {
        "taskId",
        "capability",
        "regionCode",
        "status",
        "deadlineRef",
    }
    with TenantUnitOfWork(client.app.state.runtime_engine, tenant) as session:
        from orca.application.network import Network

        task = dict(Network(session, tenant, project, principal).task_row(uuid_from(task_id)))
        parent = network_worker.event_row(session, tenant, task["id"], "task")
    assert parent["receipt"]["arkiv_v1"]["entity_key"] == task_key
    legacy = public_event(
        task=task,
        kind="claim",
        actor=str(provider.id),
        binding_version=str(provider.binding_version),
        subject=str(provider.id),
        generation=0,
        parent_key=parent["receipt"]["entity_key"],
        reference_digest=task["recipe_digest"],
        operation_id=str(uuid4()),
        issued_at=task["created_at"].isoformat(),
        private_key=client.test_provider_keys[provider.id],
        key_id=provider.key_id,
    )
    legacy_records[legacy_claim_key] = chain.record(legacy_claim_key, legacy, provider)
    path = f"/v1/network/tasks/{task_id}/claims/import"
    body = {"provider_id": str(provider.id), "entity_key": legacy_claim_key}
    assert post(client, path, body, project).status_code == 409
    body["arkiv_v1_entity_key"] = claim_key
    correct_attributes = public_claim_attributes(task["id"], provider)
    v1_records[claim_key] = {
        "entity_key": claim_key,
        "attributes": {**correct_attributes, "agentId": str(uuid4())},
    }
    handle(post(client, path, body, project), "failed")
    v1_records[claim_key]["attributes"] = correct_attributes
    handle(post(client, path, body, project), "succeeded")
    details = client.get(f"/v1/network/tasks/{task_id}", params={"project_id": str(project)}).json()
    assert len(details["claims"]) == 1

    dispatched = []

    def hold_dispatch(*args):
        dispatched.append(args[3])
        raise httpx.ReadTimeout("test transport intercept after assignment publication")

    monkeypatch.setattr(network_worker, "exchange", hold_dispatch)
    handle(
        post(
            client,
            f"/v1/network/tasks/{task_id}/assignments",
            {"claim_id": details["claims"][0]["id"], "expected_generation": 0},
            project,
        ),
        "queued",
    )
    assert len(intents) == 2 and len(dispatched) == 1
    assert intents[1]["kind"] == "assignment"
    assert intents[1]["parent"]["key"] == task_key
    assert intents[1]["attributes"]["claimRef"] == claim_key
    assert (
        intents[1]["attributes"]["assignmentDigest"]
        == dispatched[0]["assignment"]["content_digest"]
    )
    assert dispatched[0]["requester_event"]["receipt"]["arkiv_v1"]["entity_key"] == assignment_key
