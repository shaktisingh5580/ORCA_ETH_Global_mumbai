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
from orca.api.routers import companion_reply
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
    for path in ("/v1/me/preferences", "/v1/me/memory", "/v1/me/memory/{memory_id}"):
        assert path not in default and path in enabled


def check_preferences(client, params, acting, peer, other_org, admin):
    prefs, memory = "/v1/me/preferences", "/v1/me/memory"
    location = {"latitude": 20.5, "longitude": 68.5}

    def patch(version, **body):
        return client.patch(prefs, params=params, json=body, headers={"If-Match": f'"{version}"'})

    assert client.get(prefs, params=params).headers["etag"] == '"1"'
    assert client.patch(prefs, params=params, json={"units": "nautical"}).status_code == 428
    assert patch(1, location=location).status_code == 409
    assert patch(1, location_consent="true", location=location).status_code == 422
    assert (
        patch(1, location_consent=True, location={"latitude": 91, "longitude": 0}).status_code
        == 422
    )
    saved = patch(1, units="nautical", location_consent=True, location=location)
    assert saved.status_code == 200, saved.text
    assert saved.headers["etag"] == '"2"'
    entry = client.get(memory, params=params).json()["items"][0]
    assert entry["usable_as_live_observation"] is False
    assert entry["latitude"] == location["latitude"]
    resource = f"{memory}/{entry['id']}"
    original = acting[0]
    for other in (
        Principal(peer, original.issuer, original.token_scopes),
        Principal(original.subject, "issuer-b", original.token_scopes),
    ):
        acting[0] = other
        assert client.get(memory, params=params).json()["items"] == []
        assert (
            client.delete(resource, params=params, headers={"If-Match": '"1"'}).status_code == 404
        )
    acting[0] = original
    assert client.get(memory, params={"organization_id": str(other_org)}).json()["items"] == []
    assert patch(1, location_consent=False).status_code == 412
    withdrew = patch(2, location_consent=False)
    assert withdrew.json() == {
        "units": "nautical",
        "location_consent": False,
        "location": None,
        "version": 3,
    }
    with psycopg.connect(admin) as db:
        assert db.execute(
            """SELECT location_id, latitude, longitude, location_consent
            FROM research.companion_preferences WHERE owner_subject=%s""",
            (original.subject,),
        ).fetchall() == [(None, None, None, False)]
    assert client.get(memory, params=params).json()["items"] == []
    assert patch(3, location=location).status_code == 409
    saved_again = patch(3, location_consent=True, location=location)
    identifier = saved_again.json()["location"]["id"]
    assert identifier != entry["id"]
    removed = client.delete(f"{memory}/{identifier}", params=params, headers={"If-Match": '"4"'})
    assert removed.status_code == 204
    assert removed.headers["etag"] == '"5"'
    remaining = client.get(prefs, params=params).json()
    assert remaining["location_consent"] is True and remaining["location"] is None


@pytest.mark.live
@pytest.mark.enable_socket
def test_companion_conversation_isolation_pagination_and_revision(monkeypatch):
    admin, runtime, tenant, other_tenant, _, _ = prepare_database()
    subject, peer = str(uuid4()), str(uuid4())
    with psycopg.connect(admin) as db:
        orgs = dict(db.execute("SELECT tenant_id,id FROM iam.organizations").fetchall())
        for scope, user in ((tenant, subject), (tenant, peer), (other_tenant, subject)):
            db.execute(
                "INSERT INTO iam.memberships(id,tenant_id,subject,role) VALUES(%s,%s,%s,'viewer')",
                (uuid4(), scope, user),
            )

    app = create_app(
        Settings(
            monitoring_token=TOKEN,
            database_url=runtime,
            companion_enabled=True,
            companion_model_enabled=True,
            gemini_api_key="test-key",
            companion_model_user_per_minute=1,
            companion_model_organization_per_minute=2,
        )
    )
    acting = [Principal(subject, "issuer-a", frozenset({"org:read"}))]
    url = "/v1/conversations"
    params = {"organization_id": str(orgs[tenant])}
    try:
        with TestClient(app) as client:
            assert client.get(url, params=params).status_code == 503
            app.dependency_overrides[current_principal] = lambda: acting[0]
            check_preferences(client, params, acting, peer, orgs[other_tenant], admin)
            created = client.post(url, params=params, json={"title": "Private investigation"})
            assert created.status_code == 201, created.text
            assert created.headers["etag"] == '"1"'
            assert created.headers["cache-control"] == "no-store"
            resource = f"{url}/{created.json()['id']}"
            artifact = str(uuid4())
            allowed = True
            from p06_evidence import insufficient_manifest

            from orca.contracts.science_evidence import ScienceEvidenceManifest

            fixture = insufficient_manifest()
            values = fixture.model_dump(exclude={"manifest_digest"})
            values.update(
                inputs=fixture.inputs,
                source_mode="preserved_real",
                result_status="supported",
                method_approval_status="approved",
                source_qualified=True,
                baseline_qualified=True,
                absolute_tolerance_celsius=0.01,
            )
            manifest = ScienceEvidenceManifest.build(**values)
            monkeypatch.setattr(
                companion_reply,
                "accessible_manifest",
                lambda repo, identifier: (
                    manifest if allowed and str(identifier) == artifact else None
                ),
            )
            calls = []

            def classify(*args):
                calls.append(args[-1])
                return "coverage"

            monkeypatch.setattr(companion_reply, "classify", classify)
            reply_body = {
                "artifact_id": artifact,
                "question": "What years?",
                "share_question_with_model": True,
            }
            reply = client.post(resource + "/reply", params=params, json=reply_body)
            assert reply.json()["status"] == "answered", reply.text
            assert reply.json()["id"] is not None
            assert client.get(resource + "/replies", params=params).json()["items"] == [
                reply.json()
            ]
            allowed = False
            assert client.get(resource + "/replies", params=params).json()["items"] == []
            allowed = True

            def denials():
                denied = client.post(resource + "/reply", params=params, json=reply_body)
                assert denied.status_code == 429 and denied.headers["retry-after"] == "60"

            # The failed user reservation must roll back the organization increment,
            # leaving capacity for the next member.
            denials()
            first = acting[0]
            acting[0] = Principal(peer, first.issuer, first.token_scopes)
            peer_conversation = client.post(url, params=params, json={}).json()["id"]
            peer_path = f"{url}/{peer_conversation}/reply"
            assert client.post(peer_path, params=params, json=reply_body).status_code == 200
            acting[0] = Principal(first.subject, "issuer-b", first.token_scopes)
            third = client.post(url, params=params, json={}).json()["id"]
            assert (
                client.post(f"{url}/{third}/reply", params=params, json=reply_body).status_code
                == 429
            )
            acting[0] = first
            assert len(calls) == 2
            with psycopg.connect(admin) as db:
                assert db.execute(
                    """SELECT used FROM research.companion_model_counters
                    WHERE tenant_id=%s AND subject=''""",
                    (tenant,),
                ).fetchone() == (2,)
                db.execute("""UPDATE research.companion_model_counters
                    SET window_start = date_trunc('minute', clock_timestamp()) - interval '1 minute'""")
            assert (
                client.post(resource + "/reply", params=params, json=reply_body).status_code == 200
            )
            assert len(calls) == 3
            with psycopg.connect(runtime) as db:
                assert db.execute("SELECT count(*) FROM research.companion_replies").fetchone() == (
                    0,
                )
            assert client.post(url, params=params, json={}).status_code == 201
            page = client.get(url, params={**params, "limit": 1}).json()
            assert len(page["items"]) == 1
            second = client.get(
                url, params={**params, "limit": 1, "cursor": page["next_cursor"]}
            ).json()
            assert second["items"][0]["id"] != page["items"][0]["id"]
            assert second["next_cursor"] is None
            messages = resource + "/messages"
            checkpoint = resource + "/checkpoint"
            key = {"Idempotency-Key": str(uuid4())}
            body = {"content": "Compare the saved coastal observations"}
            assert client.get(checkpoint, params=params).status_code == 404
            assert client.post(messages, params=params, json=body).status_code == 422
            saved = client.post(messages, params=params, json=body, headers=key)
            assert saved.status_code == 201, saved.text
            assert saved.json()["sequence"] == 1
            assert saved.json()["provenance"] == "untrusted_user_input"
            assert (
                client.post(messages, params=params, json=body, headers=key).json() == saved.json()
            )
            assert (
                client.post(
                    messages, params=params, json={"content": "Different"}, headers=key
                ).status_code
                == 409
            )
            assert (
                client.post(
                    messages,
                    params=params,
                    json={"content": "\x00"},
                    headers={"Idempotency-Key": str(uuid4())},
                ).status_code
                == 422
            )
            assert (
                client.post(
                    messages, params=params, json=body, headers={"Idempotency-Key": str(uuid4())}
                ).json()["sequence"]
                == 2
            )
            first_message = client.get(messages, params={**params, "limit": 1}).json()
            assert first_message["next_cursor"] == 1
            assert (
                client.get(messages, params={**params, "cursor": 1}).json()["items"][0]["sequence"]
                == 2
            )
            assert client.get(checkpoint, params=params).json()["message_sequence"] == 2
            with psycopg.connect(admin) as db:
                db.execute("""UPDATE research.companion_checkpoints
                    SET updated_at=clock_timestamp()-interval '2 hours',
                        expires_at=clock_timestamp()-interval '1 hour'""")
            assert client.get(checkpoint, params=params).status_code == 404
            assert client.patch(resource, params=params, json={"archive": True}).status_code == 428
            changed = client.patch(
                resource, params=params, json={"archive": True}, headers={"If-Match": '"1"'}
            )
            assert changed.status_code == 200 and changed.json()["archived"] is True
            assert changed.headers["etag"] == '"2"'
            assert client.post(messages, params=params, json=body, headers=key).status_code == 409
            assert len(client.get(messages, params=params).json()["items"]) == 2
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
                assert client.get(messages, params=params).status_code == 404
                assert client.get(resource + "/replies", params=params).status_code == 404
                assert client.get(checkpoint, params=params).status_code == 404
                visible = client.get(url, params=params).json()["items"]
                assert [item["id"] for item in visible] == [
                    peer_conversation if other.subject == peer else third
                ]
                acting[0] = original
            with psycopg.connect(admin) as db:
                db.execute(
                    "DELETE FROM iam.memberships WHERE tenant_id=%s AND subject=%s",
                    (tenant, subject),
                )
            assert client.get(resource, params=params).status_code == 404
    finally:
        app.state.runtime_engine.dispose()
