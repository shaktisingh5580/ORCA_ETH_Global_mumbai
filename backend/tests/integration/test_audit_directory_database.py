"""Purpose: verify P08 audit and P09 directory reads against disposable PostgreSQL.
Behavior: assert tenant isolation, event redaction and unverified service status.
Refs: main_implementation plan.md P08.1/P09.1; api design.md §§3.9–3.10; Phase: P08/P09.
"""

import base64
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException, Request
from live_database import prepare_database
from psycopg.types.json import Jsonb

from orca.api.routers.federation import (
    get_agent,
    get_capabilities,
    get_endpoints,
    list_agents,
    revise_agent,
)
from orca.api.routers.operations import organization_audit_events
from orca.contracts.agents import AgentDraft, AgentRevision
from orca.domain.identity import Principal
from orca.infrastructure.db.engine import create_runtime_engine


@pytest.mark.live
def test_scoped_audit_and_unverified_directory() -> None:
    admin, runtime, tenant, other_tenant, _, _ = prepare_database()
    subject, other_subject = str(uuid4()), str(uuid4())
    event, other_event, resource, agent, binding = [uuid4() for _ in range(5)]
    with psycopg.connect(admin) as connection:
        organization = connection.execute(
            "SELECT id FROM iam.organizations WHERE tenant_id=%s", (tenant,)
        ).fetchone()[0]
        other_organization = connection.execute(
            "SELECT id FROM iam.organizations WHERE tenant_id=%s", (other_tenant,)
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO iam.memberships(id,tenant_id,subject,role) VALUES"
            "(%s,%s,%s,'org_owner'),(%s,%s,%s,'org_owner')",
            (uuid4(), tenant, subject, uuid4(), other_tenant, other_subject),
        )
        for event_id, event_tenant, actor in [
            (event, tenant, subject),
            (other_event, other_tenant, other_subject),
        ]:
            connection.execute(
                "INSERT INTO iam.membership_audit"
                "(id,tenant_id,actor_subject,member_id,event) VALUES(%s,%s,%s,%s,%s)",
                (
                    event_id,
                    event_tenant,
                    actor,
                    resource,
                    Jsonb({"kind": "file_download", "secret": "must-not-leak"}),
                ),
            )
        registration = {
            "name": "External ocean service",
            "capability": "temperature_summary_v1",
            "operator_mode": "self_declared_external",
            "endpoint": "https://example.org/a2a",
            "key_id": "test-service",
            "public_key": "public-test-key",
            "card_digest": "sha256:" + "a" * 64,
        }
        connection.execute(
            "INSERT INTO federation.registrations"
            "(id,tenant_id,organization_id,created_by,idempotency_key,request_digest,binding_version,state)"
            " VALUES(%s,%s,%s,%s,'audit-directory',%s,%s,'active')",
            (agent, tenant, organization, subject, "sha256:" + "a" * 64, binding),
        )
        connection.execute(
            "INSERT INTO federation.registration_versions"
            "(id,tenant_id,organization_id,agent_id,registration) VALUES(%s,%s,%s,%s,%s)",
            (binding, tenant, organization, agent, Jsonb(registration)),
        )
    database = create_runtime_engine(runtime)
    try:
        request = Request(
            {"type": "http", "app": SimpleNamespace(state=SimpleNamespace(runtime_engine=database))}
        )
        principal = Principal(
            subject, "test", frozenset({"audit:read", "network:read", "org:read", "org:write"})
        )
        audit = organization_audit_events(
            organization,
            request,
            principal,
            start=None,
            end=None,
            actor=None,
            event_type=None,
            cursor=None,
            limit=20,
        )
        assert [item.id for item in audit.items] == [event]
        assert "must-not-leak" not in audit.model_dump_json()
        directory = list_agents(
            organization,
            request,
            principal,
            capability=None,
            provider_mode=None,
            verified_only=False,
            cursor=None,
            limit=20,
        )
        assert [item.id for item in directory.items] == [agent]
        assert not directory.items[0].research_dispatch_enabled
        assert not directory.items[0].institutional_affiliation_verified
        detail = get_agent(agent, organization, request, principal)
        assert detail.endpoint == "https://example.org/a2a"
        assert not detail.research_dispatch_enabled
        capabilities = get_capabilities(agent, organization, request, principal)
        assert capabilities.items[0].capability_id == "temperature_summary_v1"
        assert not capabilities.items[0].approved_scientific_method
        endpoints = get_endpoints(agent, organization, request, principal)
        assert endpoints.items[0].card_url == "https://example.org/.well-known/agent-card.json"
        assert not endpoints.items[0].research_dispatch_enabled
        revised = revise_agent(
            agent,
            organization,
            AgentRevision(
                expected_version=detail.version,
                registration=AgentDraft(
                    name=detail.name,
                    endpoint="https://new.example.org/a2a",
                    public_key=base64.urlsafe_b64encode(bytes(32)).decode().rstrip("="),
                    key_id="test-service-v2",
                    card_digest="sha256:" + "b" * 64,
                    capability="temperature_summary_v1",
                    operator_mode="self_declared_external",
                ),
            ),
            request,
            principal,
        )
        assert revised.state == "draft"
        assert revised.version == detail.version + 1
        assert revised.endpoint == "https://new.example.org/a2a"
        assert not revised.service_eligible
        with pytest.raises(HTTPException) as concealed:
            get_agent(uuid4(), organization, request, principal)
        assert concealed.value.status_code == 404
        with pytest.raises(HTTPException) as denied:
            organization_audit_events(
                other_organization,
                request,
                principal,
                start=None,
                end=None,
                actor=None,
                event_type=None,
                cursor=None,
                limit=20,
            )
        assert denied.value.status_code == 404
    finally:
        database.dispose()
