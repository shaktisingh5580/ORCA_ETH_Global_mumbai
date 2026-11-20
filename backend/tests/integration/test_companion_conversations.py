"""Purpose: verify opt-in companion conversation routes against PostgreSQL.
Behavior: prove owner visibility, ETags, pagination and tenant membership revocation.
Refs: main implementation plan P12.1; A-API §3.13; W-MEM6 §6.
"""

from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from live_database import prepare_database

from orca.api.app import create_app
from orca.api.dependencies import current_principal
from orca.config import Settings
from orca.domain.identity import Principal

TOKEN = "abcdefghijklmnopqrstuvwxyz0123456789"


def test_companion_is_disabled_by_default():
    default = create_app(Settings(monitoring_token=TOKEN)).openapi()["paths"]
    enabled = create_app(Settings(monitoring_token=TOKEN, companion_enabled=True)).openapi()[
        "paths"
    ]
    assert "/v1/conversations" not in default
    assert "/v1/conversations" in enabled


@pytest.mark.live
@pytest.mark.enable_socket
def test_companion_conversation_isolation_pagination_and_revision():
    admin, runtime, tenant, other_tenant, _, _ = prepare_database()
    subject, peer = str(uuid4()), str(uuid4())
    with psycopg.connect(admin) as db:
        orgs = dict(db.execute("SELECT tenant_id,id FROM iam.organizations").fetchall())
        for scope, user in ((tenant, subject), (tenant, peer), (other_tenant, subject)):
            db.execute(
                "INSERT INTO iam.memberships(id,tenant_id,subject,role) VALUES(%s,%s,%s,'viewer')",
                (uuid4(), scope, user),
            )

    app = create_app(Settings(monitoring_token=TOKEN, database_url=runtime, companion_enabled=True))
    acting = [Principal(subject, "issuer-a", frozenset({"org:read"}))]
    url = "/v1/conversations"
    params = {"organization_id": str(orgs[tenant])}
    try:
        with TestClient(app) as client:
            assert client.get(url, params=params).status_code == 503
            app.dependency_overrides[current_principal] = lambda: acting[0]
            created = client.post(url, params=params, json={"title": "Private investigation"})
            assert created.status_code == 201, created.text
            assert created.headers["etag"] == '"1"'
            assert created.headers["cache-control"] == "no-store"
            resource = f"{url}/{created.json()['id']}"
            assert client.post(url, params=params, json={}).status_code == 201
            page = client.get(url, params={**params, "limit": 1}).json()
            assert len(page["items"]) == 1
            second = client.get(
                url, params={**params, "limit": 1, "cursor": page["next_cursor"]}
            ).json()
            assert second["items"][0]["id"] != page["items"][0]["id"]
            assert second["next_cursor"] is None
            assert client.patch(resource, params=params, json={"archive": True}).status_code == 428
            changed = client.patch(
                resource, params=params, json={"archive": True}, headers={"If-Match": '"1"'}
            )
            assert changed.status_code == 200 and changed.json()["archived"] is True
            assert changed.headers["etag"] == '"2"'
            assert (
                client.patch(
                    resource, params=params, json={"archive": False}, headers={"If-Match": '"1"'}
                ).status_code
                == 412
            )
            assert (
                client.get(
                    resource, params={"organization_id": str(orgs[other_tenant])}
                ).status_code
                == 404
            )
            for other in (
                Principal(peer, "issuer-a", acting[0].token_scopes),
                Principal(subject, "issuer-b", acting[0].token_scopes),
            ):
                original, acting[0] = acting[0], other
                assert client.get(resource, params=params).status_code == 404
                assert client.get(url, params=params).json()["items"] == []
                acting[0] = original
            with psycopg.connect(admin) as db:
                db.execute(
                    "DELETE FROM iam.memberships WHERE tenant_id=%s AND subject=%s",
                    (tenant, subject),
                )
            assert client.get(resource, params=params).status_code == 404
    finally:
        app.state.runtime_engine.dispose()
