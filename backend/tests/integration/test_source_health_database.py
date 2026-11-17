"""Purpose: verify P08 source-health reads from real tenant-scoped probe rows.
Behavior: seed disposable catalog versions and access evidence, then check RLS and labels.
Refs: main_implementation plan.md P08.1; api design.md §3.9; Phase: P08.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from fastapi import Request
from live_database import prepare_database
from psycopg.types.json import Jsonb

from orca.api.routers.operations import source_health
from orca.contracts.source_probes import SourceProbeEvidence
from orca.domain.identity import Principal
from orca.infrastructure.db.engine import create_runtime_engine


@pytest.mark.live
def test_source_health_access_evidence_is_not_science_validation() -> None:
    admin, runtime, tenant, other_tenant, _, _ = prepare_database()
    organization, product, checked, unchecked, other_product = [uuid4() for _ in range(5)]
    other_version = uuid4()
    subject = str(uuid4())
    evidence = SourceProbeEvidence(
        response_digest="sha256:" + "a" * 64,
        response_bytes=128,
        retrieved_at=datetime.now(timezone.utc),
        metadata_matches_pinned_version=True,
        variable_count=1,
    )
    with psycopg.connect(admin) as connection:
        organization = connection.execute(
            "SELECT id FROM iam.organizations WHERE tenant_id=%s", (tenant,)
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO iam.memberships(id,tenant_id,subject,role) VALUES(%s,%s,%s,'source_operator')",
            (uuid4(), tenant, subject),
        )
        connection.execute(
            "INSERT INTO research.catalog_products(id,tenant_id,provider_id,source_product_id,kind) "
            "VALUES(%s,%s,'NOAA','SST','observation'),(%s,%s,'NOAA','OTHER','observation')",
            (product, tenant, other_product, other_tenant),
        )
        connection.execute(
            "INSERT INTO research.catalog_versions(id,tenant_id,product_id,source_version,metadata_digest,manifest) "
            "VALUES(%s,%s,%s,'v1',%s,%s),(%s,%s,%s,'v2',%s,%s),(%s,%s,%s,'v1',%s,%s)",
            (
                checked,
                tenant,
                product,
                "sha256:" + "b" * 64,
                Jsonb({}),
                unchecked,
                tenant,
                product,
                "sha256:" + "c" * 64,
                Jsonb({}),
                other_version,
                other_tenant,
                other_product,
                "sha256:" + "d" * 64,
                Jsonb({}),
            ),
        )
        connection.execute(
            "INSERT INTO research.source_probes(id,tenant_id,version_id,idempotency_key,request_hash,payload,status,evidence) "
            "VALUES(%s,%s,%s,'source-health-test',%s,%s,'succeeded',%s)",
            (
                uuid4(),
                tenant,
                checked,
                "e" * 64,
                Jsonb({}),
                Jsonb(evidence.model_dump(mode="json")),
            ),
        )
    database = create_runtime_engine(runtime)
    try:
        request = Request(
            {"type": "http", "app": SimpleNamespace(state=SimpleNamespace(runtime_engine=database))}
        )
        principal = Principal(subject, "test", frozenset({"source:manage"}))
        page = source_health(
            organization, request, principal, provider_id="NOAA", cursor=None, limit=20
        )
        assert len(page.items) == 2
        by_id = {item.distribution_id: item for item in page.items}
        assert by_id[checked].probe_status == "succeeded"
        assert by_id[checked].metadata_access_passed is True
        assert by_id[checked].science_validated is False
        assert by_id[unchecked].probe_status == "never_checked"
        assert by_id[unchecked].metadata_access_passed is False
        assert other_version not in by_id
        first = source_health(
            organization, request, principal, provider_id="NOAA", cursor=None, limit=1
        )
        assert first.next_cursor is not None
        second = source_health(
            organization, request, principal, provider_id="NOAA", cursor=first.next_cursor, limit=1
        )
        assert {first.items[0].distribution_id, second.items[0].distribution_id} == {
            checked,
            unchecked,
        }
        assert second.next_cursor is None
    finally:
        database.dispose()
