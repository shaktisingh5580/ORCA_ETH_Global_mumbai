"""Purpose: prove tabular persistence, approvals, immutable retries and project isolation.
Behavior: use only the guarded disposable Postgres test database, never production.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A01 gate; Phase: A01.
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
from orca.config import Settings
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork


@pytest.mark.live
@pytest.mark.enable_socket
def test_tabular_http_persistence_isolation_and_immutability():
    admin, runtime, tenant, other, project, foreign = prepare_database()
    subject = str(uuid4())
    with psycopg.connect(admin) as connection:
        connection.execute(
            "INSERT INTO iam.memberships(id,tenant_id,subject,role) VALUES(%s,%s,%s,'org_owner')",
            (uuid4(), tenant, subject),
        )
    app = create_app(
        Settings(
            _env_file=None,
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
    try:
        with TestClient(app) as client:
            path = f"/v1/projects/{project}/datasets"
            body = {
                "name": "Synthetic control",
                "format": "csv",
                "content": "time,temp\n2025-01-01T00:00:00Z,27\n",
                "source_mode": "synthetic_demo",
            }
            first = client.post(path, json=body)
            assert first.status_code == 201, first.text
            dataset = first.json()["id"]
            assert client.post(path, json=body).json()["id"] == dataset
            mapping = {"timestamp_column": "time", "value_column": "temp", "unit": "C"}
            assert (
                client.post(f"{path}/{dataset}/preview", json=mapping).json()["accepted_rows"] == 1
            )
            norm = f"{path}/{dataset}/normalizations"
            assert client.post(norm, json={"mapping": mapping, "approve": False}).status_code == 422
            saved = client.post(norm, json={"mapping": mapping, "approve": True})
            assert saved.status_code == 201, saved.text
            assert (
                client.post(norm, json={"mapping": mapping, "approve": True}).json()["id"]
                == saved.json()["id"]
            )
            assert len(client.get(norm).json()) == 1
            assert client.get(f"/v1/projects/{foreign}/datasets").status_code == 404
            app.dependency_overrides.pop(current_principal)
            app.state.oidc_verifier = object()  # Missing bearer is rejected before verification.
            assert client.get(path).status_code == 401
        with TenantUnitOfWork(app.state.runtime_engine, other) as session:
            assert (
                session.execute(text("SELECT count(*) FROM research.tabular_datasets")).scalar_one()
                == 0
            )
        with (
            pytest.raises(DBAPIError),
            TenantUnitOfWork(app.state.runtime_engine, tenant) as session,
        ):
            session.execute(text("UPDATE research.tabular_datasets SET name='tampered'"))
    finally:
        app.state.runtime_engine.dispose()
