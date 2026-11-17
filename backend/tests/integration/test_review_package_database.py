"""Purpose: verify P07 portable review assembly from immutable local database rows.
Behavior: seed test-only evidence and exercise owner, external grant, and revocation boundaries.
Refs: main_implementation plan.md P07.4; api design.md §§3.8, 4.5; Phase: P07.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from p06_evidence import insufficient_manifest
from sqlalchemy import create_engine, text

from orca.application.review_export import assemble_review_export, verify_review_export
from orca.contracts.science_evidence import ScienceEvidenceManifest
from orca.domain.identity import Principal
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork


@pytest.mark.live
def test_review_package_real_rows_and_revoked_external_access() -> None:
    admin_url = os.environ.get("ORCA_TEST_DATABASE_URL", "")
    runtime_url = os.environ.get("ORCA_TEST_RUNTIME_DATABASE_URL", "")
    if not admin_url or not runtime_url:
        pytest.skip("disposable local Postgres unavailable")
    if any(
        "@127.0.0.1:" not in url or not url.endswith("/orca_test")
        for url in (admin_url, runtime_url)
    ):
        pytest.fail("review package requires disposable localhost orca_test")
    backend = Path(__file__).resolve().parents[2]
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(backend / "alembic.ini"), "upgrade", "head"],
        cwd=backend,
        env=dict(os.environ, ORCA_DATABASE_URL=admin_url),
        capture_output=True,
        check=True,
        timeout=120,
    )
    admin = create_engine(admin_url.replace("postgresql://", "postgresql+psycopg://", 1))
    runtime = create_engine(runtime_url.replace("postgresql://", "postgresql+psycopg://", 1))
    tenant, org, project = uuid4(), uuid4(), uuid4()
    investigation, plan, run, correction_run = uuid4(), uuid4(), uuid4(), uuid4()
    artifact, correction, challenge, grant = uuid4(), uuid4(), uuid4(), uuid4()
    owner, external = "review-package-owner", "external-reviewer"
    source = insufficient_manifest()
    values = source.model_dump(mode="python", exclude={"manifest_digest"})
    values["inputs"] = source.inputs
    evidence = ScienceEvidenceManifest.build(
        **{**values, "tenant_id": tenant, "project_id": project, "created_by": owner}
    )
    digest = "sha256:" + "a" * 64
    try:
        with admin.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO iam.organizations(id,tenant_id,name,slug) VALUES(:id,:t,'review-export',:s)"
                ),
                {"id": org, "t": tenant, "s": str(tenant)},
            )
            connection.execute(
                text(
                    "INSERT INTO research.projects(id,tenant_id,slug,name) VALUES(:id,:t,:s,'review-export')"
                ),
                {"id": project, "t": tenant, "s": str(project)},
            )
            connection.execute(
                text(
                    "INSERT INTO iam.memberships(id,tenant_id,subject,role) VALUES(:id,:t,:s,'org_owner')"
                ),
                {"id": uuid4(), "t": tenant, "s": owner},
            )
            connection.execute(
                text("""INSERT INTO research.investigations
              (id,tenant_id,project_id,created_by,question,geo_scope,time_window,risk_tier,
               idempotency_key,request_digest)
              VALUES(:id,:t,:p,:actor,'Does the review export preserve immutable evidence?',
                CAST(:geo AS jsonb),CAST(:window AS jsonb),'research',:key,:digest)"""),
                {
                    "id": investigation,
                    "t": tenant,
                    "p": project,
                    "actor": owner,
                    "geo": '{"type":"Point"}',
                    "window": "{}",
                    "key": str(investigation),
                    "digest": digest,
                },
            )
            connection.execute(
                text("""INSERT INTO research.investigation_plans
              (id,tenant_id,project_id,investigation_id,revision,plan,plan_digest,
               request_digest,idempotency_key,created_by)
              VALUES(:id,:t,:p,:investigation,1,'{}',:digest,:digest,:key,:actor)"""),
                {
                    "id": plan,
                    "t": tenant,
                    "p": project,
                    "investigation": investigation,
                    "digest": digest,
                    "key": str(plan),
                    "actor": owner,
                },
            )
            for current_run in (run, correction_run):
                connection.execute(
                    text("""INSERT INTO research.compute_runs
                  (id,tenant_id,project_id,investigation_id,plan_id,plan_digest,created_by,
                   execution_budget,request_digest,idempotency_key,state,stage)
                  VALUES(:id,:t,:p,:investigation,:plan,:digest,:actor,1.00,:digest,:key,
                    'SUCCEEDED_INSUFFICIENT','completed')"""),
                    {
                        "id": current_run,
                        "t": tenant,
                        "p": project,
                        "investigation": investigation,
                        "plan": plan,
                        "digest": digest,
                        "actor": owner,
                        "key": str(current_run),
                    },
                )
            connection.execute(
                text("""INSERT INTO research.science_evidence_manifests
              (id,tenant_id,project_id,created_by,manifest,manifest_digest)
              VALUES(:id,:t,:p,:actor,CAST(:manifest AS jsonb),:digest)"""),
                {
                    "id": evidence.id,
                    "t": tenant,
                    "p": project,
                    "actor": owner,
                    "manifest": json.dumps(evidence.model_dump(mode="json")),
                    "digest": evidence.manifest_digest,
                },
            )
            for current_artifact, current_run in ((artifact, run), (correction, correction_run)):
                connection.execute(
                    text("""INSERT INTO research.run_artifacts
                  (id,tenant_id,project_id,investigation_id,run_id,evidence_manifest_id,scientific_status)
                  VALUES(:id,:t,:p,:investigation,:run,:manifest,'insufficient_evidence')"""),
                    {
                        "id": current_artifact,
                        "t": tenant,
                        "p": project,
                        "investigation": investigation,
                        "run": current_run,
                        "manifest": evidence.id,
                    },
                )
            connection.execute(
                text("""INSERT INTO research.independent_reviews
              (id,tenant_id,project_id,artifact_id,reviewer_subject,verdict,statement)
              VALUES(:id,:t,:p,:artifact,:subject,'inconclusive',:statement)"""),
                {
                    "id": uuid4(),
                    "t": tenant,
                    "p": project,
                    "artifact": artifact,
                    "subject": external,
                    "statement": "The original result needs independent source confirmation.",
                },
            )
            connection.execute(
                text("""INSERT INTO research.scientific_challenges
              (id,tenant_id,project_id,artifact_id,reviewer_subject,discrepancy_type,
               source_refs,statement)
              VALUES(:id,:t,:p,:artifact,:subject,'missing_source',CAST(:refs AS jsonb),:statement)"""),
                {
                    "id": challenge,
                    "t": tenant,
                    "p": project,
                    "artifact": artifact,
                    "subject": external,
                    "refs": json.dumps([str(uuid4())]),
                    "statement": "A reference observation is missing from the record.",
                },
            )
            connection.execute(
                text("""INSERT INTO research.challenge_responses
              (id,tenant_id,project_id,challenge_id,responder_subject,response_artifact_id,statement)
              VALUES(:id,:t,:p,:challenge,:actor,:correction,:statement)"""),
                {
                    "id": uuid4(),
                    "t": tenant,
                    "p": project,
                    "challenge": challenge,
                    "actor": owner,
                    "correction": correction,
                    "statement": "A separate immutable artifact records the response.",
                },
            )
            connection.execute(
                text("""INSERT INTO research.challenge_resolutions
              (id,tenant_id,project_id,challenge_id,resolver_subject,verdict,
               arbitration_refs,limitations)
              VALUES(:id,:t,:p,:challenge,'separate-resolver','inconclusive','[]',:limitations)"""),
                {
                    "id": uuid4(),
                    "t": tenant,
                    "p": project,
                    "challenge": challenge,
                    "limitations": "The missing observation still prevents a conclusive decision.",
                },
            )
            connection.execute(
                text("""INSERT INTO research.artifact_review_grants
              (id,tenant_id,project_id,artifact_id,reviewer_subject,granted_by,expires_at)
              VALUES(:id,:t,:p,:artifact,:subject,:actor,clock_timestamp()+interval '1 hour')"""),
                {
                    "id": grant,
                    "t": tenant,
                    "p": project,
                    "artifact": artifact,
                    "subject": external,
                    "actor": owner,
                },
            )
        key = Ed25519PrivateKey.generate()
        for principal in (
            Principal(owner, "test", frozenset({"evidence:read"})),
            Principal(external, "test", frozenset({"evidence:read"})),
        ):
            with TenantUnitOfWork(runtime, tenant) as session:
                package = assemble_review_export(
                    session,
                    tenant,
                    artifact,
                    principal,
                    signing_key=key,
                    signer_key_id="test-operator-key",
                )
                payload = verify_review_export(
                    package.document,
                    public_key=key.public_key(),
                    expected_key_id="test-operator-key",
                    expected_artifact_id=artifact,
                    expected_manifest_digest=evidence.manifest_digest,
                )
                assert len(payload["reviews"]) == 1
                assert payload["challenges"][0]["status"] == "inconclusive"
                assert payload["responses"][0]["response_artifact_id"] == str(correction)
                assert payload["resolutions"][0]["verdict"] == "inconclusive"
                assert payload["independent_institution_verified"] is False
        with admin.begin() as connection:
            connection.execute(
                text(
                    "UPDATE research.artifact_review_grants SET revoked_at=clock_timestamp() WHERE id=:id"
                ),
                {"id": grant},
            )
        with TenantUnitOfWork(runtime, tenant) as session:
            with pytest.raises(LookupError, match="not found"):
                assemble_review_export(
                    session,
                    tenant,
                    artifact,
                    Principal(external, "test", frozenset({"evidence:read"})),
                )
    finally:
        runtime.dispose()
        admin.dispose()
