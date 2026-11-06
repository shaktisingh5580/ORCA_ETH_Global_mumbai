"""Purpose: prove project authorization, revocation, scoped pagination and policy enforcement.
Behavior: call real HTTP handlers against disposable PostgreSQL using the non-owner runtime role.
Refs: API 2.2/3.2/3.3; main plan P02; Phase: P02 completion repair.
"""

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import cast
from uuid import uuid4

import boto3
import httpx
import psycopg
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from live_database import prepare_database
from pydantic import SecretStr

from orca.api.app import create_app
from orca.api.dependencies import current_principal
from orca.application.invitations import invitation_token
from orca.config import Settings
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.infrastructure.storage.uploads import UploadClient, ValidatedUploadStore
from orca.upload_worker import process_pending


@pytest.fixture
def workspace_http():
    admin, runtime, tenant, other_tenant, project, other_project = prepare_database()
    owner, scientist, reader, outsider = (str(uuid4()) for _ in range(4))
    owner_member, scientist_member, reader_member = uuid4(), uuid4(), uuid4()
    with psycopg.connect(admin) as connection:
        org = connection.execute(
            "SELECT id FROM iam.organizations WHERE tenant_id=%s", (tenant,)
        ).fetchone()[0]
        for identifier, subject, role in [
            (owner_member, owner, "org_owner"),
            (scientist_member, scientist, "principal_investigator"),
            (reader_member, reader, "viewer"),
        ]:
            connection.execute(
                "INSERT INTO iam.memberships(id,tenant_id,subject,role) VALUES(%s,%s,%s,%s)",
                (identifier, tenant, subject, role),
            )
        connection.execute(
            "INSERT INTO iam.memberships(id,tenant_id,subject,role) VALUES(%s,%s,%s,'org_owner')",
            (uuid4(), other_tenant, outsider),
        )
    app = create_app(
        Settings(
            monitoring_token="0123456789abcdefghijklmnopqrstuvwxyz",
            database_url=runtime.replace("postgresql://", "postgresql+psycopg://", 1),
        )
    )
    acting = [owner]
    app.dependency_overrides[current_principal] = lambda: Principal(
        acting[0],
        "test",
        frozenset().union(*ROLE_SCOPES.values()),
        verified_email=getattr(app.state, "test_verified_emails", {}).get(acting[0]),
    )
    with TestClient(app) as client:
        yield (
            client,
            acting,
            org,
            project,
            other_project,
            scientist,
            scientist_member,
            reader,
            reader_member,
        )
    app.state.runtime_engine.dispose()


@pytest.mark.live
@pytest.mark.enable_socket
def test_private_project_hidden_until_explicit_membership_and_revocation_is_immediate(
    workspace_http,
):
    client, acting, org, project, _, scientist, scientist_id, _, _ = workspace_http
    owner = acting[0]
    acting[0] = scientist
    assert client.get(f"/v1/research/projects/{project}").status_code == 404
    assert (
        client.get("/v1/research/projects", params={"organization_id": str(org)}).json()["items"]
        == []
    )
    acting[0] = owner
    member = client.post(
        f"/v1/research/projects/{project}/members",
        json={"principal_id": str(scientist_id), "role": "viewer"},
    )
    assert member.status_code == 201, member.text
    identifier, etag = member.json()["id"], member.headers["etag"]
    acting[0] = scientist
    assert client.get(f"/v1/research/projects/{project}").status_code == 200
    assert (
        client.patch(
            f"/v1/research/projects/{project}",
            json={"name": "Forbidden"},
            headers={"If-Match": '"1"'},
        ).status_code
        == 403
    )
    acting[0] = owner
    assert (
        client.delete(
            f"/v1/research/projects/{project}/members/{identifier}", headers={"If-Match": etag}
        ).status_code
        == 204
    )
    acting[0] = scientist
    assert client.get(f"/v1/research/projects/{project}").status_code == 404
    assert (
        client.get("/v1/research/projects", params={"organization_id": str(org)}).json()["items"]
        == []
    )


@pytest.mark.live
@pytest.mark.enable_socket
def test_creator_admin_etags_last_admin_and_no_cross_tenant_grants(workspace_http):
    client, acting, org, _, other_project, scientist, _, reader, reader_id = workspace_http
    owner = acting[0]
    acting[0] = scientist
    created = client.post(
        "/v1/research/projects",
        json={"organization_id": str(org), "slug": "new-work", "name": "New work"},
        headers={"Idempotency-Key": "new"},
    )
    assert created.status_code == 201, created.text
    project = created.json()["id"]
    assert client.get(f"/v1/research/projects/{project}").status_code == 200
    members = client.get(f"/v1/research/projects/{project}/members").json()["items"]
    assert len(members) == 1 and members[0]["role"] == "admin"
    target = f"/v1/research/projects/{project}/members/{members[0]['id']}"
    assert client.delete(target).status_code == 428
    assert client.delete(target, headers={"If-Match": '"0"'}).status_code == 412
    assert client.delete(target, headers={"If-Match": '"1"'}).status_code == 409
    assert (
        client.post(
            f"/v1/research/projects/{project}/members",
            json={"principal_id": str(uuid4()), "role": "admin"},
        ).status_code
        == 404
    )
    assert client.get(f"/v1/research/projects/{other_project}").status_code == 404
    member = client.post(
        f"/v1/research/projects/{project}/members",
        json={"principal_id": str(reader_id), "role": "admin"},
    )
    assert member.status_code == 201
    acting[0] = reader
    # A project admin grant cannot give an organization viewer write/share capability.
    assert (
        client.patch(
            f"/v1/research/projects/{project}", json={"name": "No"}, headers={"If-Match": '"1"'}
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/research/projects/{project}/members",
            json={"principal_id": str(reader_id), "role": "editor"},
        ).status_code
        == 403
    )
    acting[0] = owner
    assert (
        client.patch(
            f"/v1/organizations/{org}/members/{members[0]['principal_id']}",
            json={"status": "revoked"},
            headers={"If-Match": '"1"'},
        ).status_code
        == 200
    )
    # Removing an already-inactive administrator cannot reduce the active count.
    assert client.delete(target, headers={"If-Match": members[0]["etag"]}).status_code == 204


@pytest.mark.live
@pytest.mark.enable_socket
def test_policy_versions_platform_bounds_and_real_project_quota(workspace_http):
    client, acting, org, _, _, scientist, _, _, _ = workspace_http
    endpoint = f"/v1/organizations/{org}/policies"
    initial = client.get(endpoint)
    assert initial.status_code == 200 and initial.headers["etag"] == '"1"'
    assert client.patch(endpoint, json={"max_projects": 1}).status_code == 428
    changed = client.patch(endpoint, json={"max_projects": 1}, headers={"If-Match": '"1"'})
    assert changed.status_code == 200 and changed.headers["etag"] == '"2"'
    assert (
        client.patch(endpoint, json={"max_projects": 1001}, headers={"If-Match": '"2"'}).status_code
        == 422
    )
    assert (
        client.patch(
            endpoint, json={"source_license_enforcement": False}, headers={"If-Match": '"2"'}
        ).status_code
        == 422
    )
    assert (
        client.patch(endpoint, json={"max_projects": 2}, headers={"If-Match": '"1"'}).status_code
        == 412
    )
    assert (
        client.post(
            "/v1/research/projects",
            json={"organization_id": str(org), "slug": "blocked", "name": "Blocked"},
            headers={"Idempotency-Key": "blocked"},
        ).status_code
        == 409
    )
    acting[0] = scientist
    assert client.get(endpoint).status_code == 200
    assert (
        client.patch(endpoint, json={"max_projects": 10}, headers={"If-Match": '"2"'}).status_code
        == 403
    )


@pytest.fixture
def file_http(workspace_http):
    endpoint = os.environ.get("ORCA_TEST_S3_ENDPOINT", "")
    if not endpoint:
        pytest.skip("disposable local S3 store required")
    if not endpoint.startswith("http://127.0.0.1:"):
        pytest.fail("file HTTP integration refuses nonlocal object storage")
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["ORCA_TEST_S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["ORCA_TEST_S3_SECRET_KEY"],
        region_name="us-east-1",
        config=Config(signature_version="s3v4", request_checksum_calculation="when_required"),
    )
    store = ValidatedUploadStore(cast(UploadClient, client), "orca-research-test")
    store.ensure_bucket()
    workspace_http[0].app.state.upload_store = store
    yield workspace_http, store


def stage_bytes(
    client,
    project,
    content,
    *,
    declared=None,
    filename="observations.csv",
    media_type="text/csv",
    classification="project",
):
    digest = hashlib.sha256(declared if declared is not None else content).hexdigest()
    staged = client.post(
        f"/v1/research/projects/{project}/uploads",
        json={
            "filename": filename,
            "media_type": media_type,
            "size_bytes": len(content),
            "sha256": "sha256:" + digest,
            "classification": classification,
        },
    )
    assert staged.status_code == 201, staged.text
    part = staged.json()["parts"][0]
    uploaded = httpx.put(part["url"], content=content, headers=part["headers"], timeout=10)
    assert uploaded.status_code == 200, uploaded.text
    return staged.json()["upload_id"], "sha256:" + digest


@pytest.mark.live
@pytest.mark.enable_socket
def test_real_upload_quarantine_validation_download_and_revoked_access(file_http):
    workspace, store = file_http
    client, acting, _, project, _, scientist, scientist_id, _, _ = workspace
    owner = acting[0]
    raw = b"date,sst\n2026-01-01,26.5\n"
    identifier, digest = stage_bytes(client, project, raw)
    assert client.get(f"/v1/research/files/{identifier}").status_code == 404
    assert client.get(f"/v1/research/projects/{project}/files").json()["items"] == []
    assert (
        client.post(
            f"/v1/research/uploads/{identifier}/complete", json={"sha256": digest}
        ).status_code
        == 202
    )
    assert client.get(f"/v1/research/uploads/{identifier}").json()["status"] == "pending_validation"
    assert (
        client.post(
            f"/v1/research/files/{identifier}/download-intents", json={"purpose": "review"}
        ).status_code
        == 404
    )
    assert process_pending(client.app.state.runtime_engine, store) == 1
    assert process_pending(client.app.state.runtime_engine, store) == 0
    assert client.get(f"/v1/research/uploads/{identifier}").json()["status"] == "finalized"
    assert (
        client.get(f"/v1/research/projects/{project}/files").json()["items"][0]["sha256"] == digest
    )
    signed = client.post(
        f"/v1/research/files/{identifier}/download-intents",
        json={"purpose": "independent comparison"},
    )
    assert signed.status_code == 200
    fetched = httpx.get(signed.json()["url"], timeout=10)
    assert fetched.content == raw
    assert fetched.headers["Content-Disposition"] == "attachment"
    assert "X-Amz-Expires=60" in signed.json()["url"]
    membership = client.post(
        f"/v1/research/projects/{project}/members",
        json={"principal_id": str(scientist_id), "role": "viewer"},
    ).json()
    acting[0] = scientist
    assert client.get(f"/v1/research/files/{identifier}").status_code == 200
    acting[0] = owner
    assert (
        client.delete(
            f"/v1/research/projects/{project}/members/{membership['id']}",
            headers={"If-Match": membership["etag"]},
        ).status_code
        == 204
    )
    acting[0] = scientist
    assert client.get(f"/v1/research/files/{identifier}").status_code == 404
    assert (
        client.post(
            f"/v1/research/files/{identifier}/download-intents", json={"purpose": "review"}
        ).status_code
        == 404
    )


@pytest.mark.live
@pytest.mark.enable_socket
def test_real_bytes_override_forged_metadata_and_bad_formats_stay_hidden(file_http):
    workspace, store = file_http
    client, _, _, project, *_ = workspace
    for content, declared in [(b"x,y\n9,8\n", b"x,y\n1,2\n"), (b"x,y\n1\n", None)]:
        identifier, digest = stage_bytes(client, project, content, declared=declared)
        assert (
            client.post(
                f"/v1/research/uploads/{identifier}/complete", json={"sha256": digest}
            ).status_code
            == 202
        )
        assert process_pending(client.app.state.runtime_engine, store) == 1
        status = client.get(f"/v1/research/uploads/{identifier}").json()
        assert status["status"] == "rejected"
        assert status["rejection_code"] == "CONTENT_VALIDATION_FAILED"
        assert client.get(f"/v1/research/files/{identifier}").status_code == 404
    assert client.get(f"/v1/research/projects/{project}/files").json()["items"] == []
    identifier, digest = stage_bytes(client, project, b"x,y\n1,2\n")
    assert client.delete(f"/v1/research/uploads/{identifier}").status_code == 204
    assert (
        client.post(
            f"/v1/research/uploads/{identifier}/complete", json={"sha256": digest}
        ).status_code
        == 422
    )


@pytest.mark.live
@pytest.mark.enable_socket
def test_expired_staging_cleanup_and_retry_after_publication_rollback(file_http):
    workspace, store = file_http
    client, acting, _, project, *_ = workspace
    identifier, digest = stage_bytes(client, project, b"x,y\n1,2\n")
    assert (
        client.post(
            f"/v1/research/uploads/{identifier}/complete", json={"sha256": digest}
        ).status_code
        == 202
    )
    from sqlalchemy import text

    from orca.application.files import Files
    from orca.infrastructure.db.unit_of_work import TenantUnitOfWork

    with client.app.state.runtime_engine.connect() as connection:
        tenant = connection.execute(
            text("SELECT iam.resolve_project_tenant(:s,:p)"), {"s": acting[0], "p": project}
        ).scalar_one()
    with pytest.raises(RuntimeError):
        with TenantUnitOfWork(client.app.state.runtime_engine, tenant) as session:
            assert Files(session, tenant, store).validate(identifier)
            raise RuntimeError("simulated crash after storage publication before database commit")
    assert client.get(f"/v1/research/uploads/{identifier}").json()["status"] == "pending_validation"
    assert process_pending(client.app.state.runtime_engine, store) == 1
    assert client.get(f"/v1/research/uploads/{identifier}").json()["status"] == "finalized"
    staged, _ = stage_bytes(client, project, b"x,y\n3,4\n")
    with psycopg.connect(os.environ["ORCA_TEST_DATABASE_URL"]) as connection:
        staging_key = connection.execute(
            "SELECT object_key FROM research.uploads WHERE id=%s", (staged,)
        ).fetchone()[0]
        connection.execute(
            'UPDATE research.uploads SET metadata_json=metadata_json || \'{"expires_at":"2020-01-01T00:00:00+00:00"}\'::jsonb WHERE id=%s',
            (staged,),
        )
    assert process_pending(client.app.state.runtime_engine, store) == 1
    assert client.get(f"/v1/research/uploads/{staged}").json()["status"] == "aborted"
    with pytest.raises(ClientError):
        store.client.head_object(Bucket=store.bucket, Key=staging_key)


@pytest.mark.live
@pytest.mark.enable_socket
def test_invitation_identity_binding_idempotency_and_atomic_single_use(workspace_http):
    client, acting, org, *_ = workspace_http
    signing_key = "local-test-invitation-signing-key-abcdefghijklmnopqrstuvwxyz"
    client.app.state.invitation_signing_key = SecretStr(signing_key)
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    payload = {"email": "RESEARCHER@example.org", "role": "researcher", "expires_at": expires}
    created = client.post(
        f"/v1/organizations/{org}/invitations", json=payload, headers={"Idempotency-Key": "invite"}
    )
    assert created.status_code == 201, created.text
    repeated = client.post(
        f"/v1/organizations/{org}/invitations", json=payload, headers={"Idempotency-Key": "invite"}
    )
    assert repeated.json() == created.json()
    assert (
        client.post(
            f"/v1/organizations/{org}/invitations",
            json={**payload, "role": "org_admin"},
            headers={"Idempotency-Key": "invite"},
        ).status_code
        == 409
    )
    invitation = created.json()
    from uuid import UUID

    token = invitation_token(
        signing_key,
        UUID(invitation["id"]),
        invitation["email"],
        datetime.fromisoformat(invitation["expires_at"].replace("Z", "+00:00")),
    )
    assert token not in created.text and "token" not in invitation
    recipient = str(uuid4())
    acting[0] = recipient
    endpoint = f"/v1/invitations/{token}/accept"
    assert client.post(endpoint, json={"accept": True}).status_code == 403
    client.app.state.test_verified_emails = {recipient: "wrong@example.org"}
    assert client.post(endpoint, json={"accept": True}).status_code == 404
    client.app.state.test_verified_emails[recipient] = "researcher@example.org"
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(
            pool.map(lambda _: client.post(endpoint, json={"accept": True}).status_code, range(2))
        )
    assert sorted(statuses) == [200, 404]
    assert client.get(f"/v1/organizations/{org}").status_code == 200
    assert client.post(endpoint, json={"accept": True}).status_code == 404


@pytest.mark.live
@pytest.mark.enable_socket
def test_explicit_read_grants_expire_revoke_and_do_not_allow_write(workspace_http):
    client, acting, org, project, _, scientist, scientist_id, _, _ = workspace_http
    owner = acting[0]
    body = {
        "resource_ref": {"type": "research_project", "id": str(project), "version": "1"},
        "principal_ref": {"type": "organization_member", "id": str(scientist_id)},
        "scopes": ["project:read"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    endpoint = f"/v1/organizations/{org}/access-grants"
    created = client.post(endpoint, json=body)
    assert created.status_code == 201, created.text
    grant = created.json()
    acting[0] = scientist
    assert client.get(f"/v1/research/projects/{project}").status_code == 200
    assert (
        len(
            client.get("/v1/research/projects", params={"organization_id": str(org)}).json()[
                "items"
            ]
        )
        == 1
    )
    assert (
        client.patch(
            f"/v1/research/projects/{project}", json={"name": "No"}, headers={"If-Match": '"1"'}
        ).status_code
        == 403
    )
    acting[0] = owner
    assert (
        client.get(endpoint, params={"principal": str(scientist_id)}).json()["items"][0]["id"]
        == grant["id"]
    )
    assert client.delete(endpoint + "/" + grant["id"]).status_code == 428
    assert (
        client.delete(endpoint + "/" + grant["id"], headers={"If-Match": grant["etag"]}).status_code
        == 204
    )
    acting[0] = scientist
    assert client.get(f"/v1/research/projects/{project}").status_code == 404
    acting[0] = owner
    second = client.post(endpoint, json=body).json()
    with psycopg.connect(os.environ["ORCA_TEST_DATABASE_URL"]) as connection:
        connection.execute(
            "UPDATE iam.access_grants SET expires_at=now()-interval '1 second' WHERE id=%s",
            (second["id"],),
        )
    acting[0] = scientist
    assert client.get(f"/v1/research/projects/{project}").status_code == 404
    acting[0] = owner
    assert (
        client.post(
            endpoint,
            json={**body, "principal_ref": {"type": "organization_member", "id": str(uuid4())}},
        ).status_code
        == 404
    )
    assert client.post(endpoint, json={**body, "scopes": ["project:write"]}).status_code == 422
    assert (
        client.post(
            endpoint,
            json={**body, "resource_ref": {**body["resource_ref"], "type": "dataset_snapshot"}},
        ).status_code
        == 422
    )


@pytest.mark.live
@pytest.mark.enable_socket
def test_file_classification_survives_project_visibility_changes(file_http):
    workspace, store = file_http
    client, acting, _, project, _, scientist, scientist_id, _, _ = workspace
    owner = acting[0]
    shared_file, digest = stage_bytes(client, project, b"x,y\n1,2\n")
    confidential, private_digest = stage_bytes(
        client, project, b"x,y\n3,4\n", classification="confidential"
    )
    for identifier, checksum in [(shared_file, digest), (confidential, private_digest)]:
        assert (
            client.post(
                f"/v1/research/uploads/{identifier}/complete", json={"sha256": checksum}
            ).status_code
            == 202
        )
    assert process_pending(client.app.state.runtime_engine, store) == 2
    assert (
        client.patch(
            f"/v1/research/projects/{project}",
            json={"visibility": "organization"},
            headers={"If-Match": '"1"'},
        ).status_code
        == 200
    )
    acting[0] = scientist
    assert client.get(f"/v1/research/projects/{project}").status_code == 200
    assert client.get(f"/v1/research/projects/{project}/files").json()["items"] == []
    assert client.get(f"/v1/research/files/{confidential}").status_code == 404
    acting[0] = owner
    membership = client.post(
        f"/v1/research/projects/{project}/members",
        json={"principal_id": str(scientist_id), "role": "viewer"},
    ).json()
    acting[0] = scientist
    assert [
        row["id"] for row in client.get(f"/v1/research/projects/{project}/files").json()["items"]
    ] == [shared_file]
    assert (
        client.post(
            f"/v1/research/files/{confidential}/download-intents", json={"purpose": "review"}
        ).status_code
        == 404
    )
    acting[0] = owner
    assert (
        client.patch(
            f"/v1/research/projects/{project}/members/{membership['id']}",
            json={"role": "admin"},
            headers={"If-Match": membership["etag"]},
        ).status_code
        == 200
    )
    acting[0] = scientist
    assert client.get(f"/v1/research/files/{confidential}").status_code == 200
