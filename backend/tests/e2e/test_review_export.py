"""Purpose: verify P07 portable review evidence and package integrity signatures.
Behavior: use test-only evidence and keys; reject cross-artifact links, tampering and signature downgrade.
Refs: main_implementation plan.md P07.4; api design.md §4.5; Phase: P07.
"""

import hashlib
import json
from datetime import datetime, timezone
from functools import partial
from uuid import uuid4

import pytest
import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from p06_evidence import insufficient_manifest

from orca.application.review_export import (
    assemble_review_export,
    build_review_export,
    verify_review_export,
)
from orca.contracts.scientific_reviews import (
    ChallengeResolutionView,
    ChallengeResponseView,
    ChallengeView,
    IndependentReviewView,
)
from orca.domain.identity import Principal


def records():
    artifact_id, challenge_id = uuid4(), uuid4()
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    review = IndependentReviewView(
        id=uuid4(),
        artifact_id=artifact_id,
        reviewer_subject="separate-reviewer",
        verdict="inconclusive",
        statement="The evidence needs independent source validation.",
        reproduction_digest=None,
        independence_verified=False,
        created_at=now,
    )
    challenge = ChallengeView(
        id=challenge_id,
        artifact_id=artifact_id,
        reviewer_subject="separate-reviewer",
        discrepancy_type="missing_source",
        source_refs=[uuid4()],
        statement="A source needed for the conclusion is absent.",
        status="inconclusive",
        response_count=1,
        created_at=now,
    )
    response = ChallengeResponseView(
        id=uuid4(),
        challenge_id=challenge_id,
        responder_subject="origin-author",
        response_artifact_id=uuid4(),
        statement="A new artifact records the available evidence.",
        created_at=now,
    )
    resolution = ChallengeResolutionView(
        id=uuid4(),
        challenge_id=challenge_id,
        resolver_subject="third-reviewer",
        verdict="inconclusive",
        arbitration_refs=[],
        limitations="The source is still absent and the claim remains open.",
        independence_verified=False,
        created_at=now,
    )
    return artifact_id, review, challenge, response, resolution


def test_review_portable_and_signed_if_supported() -> None:
    evidence = insufficient_manifest()
    artifact, review, challenge, response, resolution = records()
    key = Ed25519PrivateKey.generate()
    package = build_review_export(
        artifact_id=artifact,
        evidence=evidence,
        reviews=(review,),
        challenges=(challenge,),
        responses=(response,),
        resolutions=(resolution,),
        signing_key=key,
        signer_key_id="operator-key-1",
    )
    assert package.signed
    verify = partial(
        verify_review_export,
        expected_artifact_id=artifact,
        expected_manifest_digest=evidence.manifest_digest,
    )
    payload = verify(
        package.document, public_key=key.public_key(), expected_key_id="operator-key-1"
    )
    assert payload["evidence_manifest_digest"] == evidence.manifest_digest
    assert payload["independent_institution_verified"] is False
    assert payload["signature_scope"] == "package_integrity_only"
    assert payload["resolutions"][0]["verdict"] == "inconclusive"


def test_review_export_rejects_tamper_and_signature_downgrade() -> None:
    artifact, review, challenge, response, resolution = records()
    key = Ed25519PrivateKey.generate()
    kwargs = dict(
        artifact_id=artifact,
        evidence=insufficient_manifest(),
        reviews=(review,),
        challenges=(challenge,),
        responses=(response,),
        resolutions=(resolution,),
    )
    package = build_review_export(**kwargs, signing_key=key, signer_key_id="operator-key-1")
    verify = partial(
        verify_review_export,
        expected_artifact_id=artifact,
        expected_manifest_digest=kwargs["evidence"].manifest_digest,
    )
    modified = json.loads(package.document)
    modified["payload"]["reviews"][0]["verdict"] = "agrees"
    with pytest.raises(ValueError, match="digest mismatch"):
        verify(
            json.dumps(modified).encode(),
            public_key=key.public_key(),
            expected_key_id="operator-key-1",
        )
    modified["payload_digest"] = (
        "sha256:" + hashlib.sha256(rfc8785.dumps(modified["payload"])).hexdigest()
    )
    with pytest.raises(ValueError, match="package signature"):
        verify(
            json.dumps(modified).encode(),
            public_key=key.public_key(),
            expected_key_id="operator-key-1",
        )
    with pytest.raises(ValueError, match="package signature"):
        verify(
            package.document,
            public_key=Ed25519PrivateKey.generate().public_key(),
            expected_key_id="operator-key-1",
        )
    with pytest.raises(ValueError, match="trusted package signing key"):
        verify(package.document, public_key=key.public_key(), expected_key_id="another-key")
    with pytest.raises(ValueError, match="context mismatch"):
        verify_review_export(
            package.document,
            public_key=key.public_key(),
            expected_key_id="operator-key-1",
            expected_artifact_id=uuid4(),
            expected_manifest_digest=kwargs["evidence"].manifest_digest,
        )
    unsigned = build_review_export(**kwargs)
    with pytest.raises(ValueError, match="signature requirement"):
        verify(unsigned.document, public_key=key.public_key(), expected_key_id="operator-key-1")
    with pytest.raises(ValueError, match="another artifact"):
        build_review_export(**dict(kwargs, artifact_id=uuid4()))


def test_review_export_assembles_scoped_immutable_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = insufficient_manifest()
    artifact, review, challenge, response, resolution = records()
    monkeypatch.setattr("orca.application.review_export.authorize_project", lambda *_args: None)

    class Result:
        def __init__(self, records: list[dict[str, object]]):
            self.records = records

        def mappings(self):
            return self

        def one_or_none(self):
            return self.records[0] if self.records else None

        def all(self):
            return self.records

    class Session:
        def execute(self, statement, params):
            assert params["tenant"] == evidence.tenant_id
            assert params["artifact"] == artifact
            query = str(statement)
            if "FROM research.run_artifacts a" in query:
                return Result(
                    [
                        {
                            "project_id": evidence.project_id,
                            "evidence_manifest_id": evidence.id,
                            "scientific_status": evidence.result_status,
                            "manifest": evidence.model_dump(mode="json"),
                            "manifest_digest": evidence.manifest_digest,
                        }
                    ]
                )
            assert params["project"] == evidence.project_id
            if "FROM research.independent_reviews" in query:
                return Result([review.model_dump(mode="json")])
            if "FROM research.scientific_challenges c" in query:
                return Result(
                    [
                        {
                            **challenge.model_dump(mode="json", exclude={"etag", "status"}),
                            "resolution_verdict": "inconclusive",
                        }
                    ]
                )
            if "FROM research.challenge_responses r" in query:
                return Result([response.model_dump(mode="json")])
            if "FROM research.challenge_resolutions z" in query:
                return Result([resolution.model_dump(mode="json")])
            raise AssertionError(query)

    package = assemble_review_export(
        Session(),
        evidence.tenant_id,
        artifact,
        Principal("reader", "test", frozenset({"evidence:read"})),
    )
    payload = verify_review_export(
        package.document,
        public_key=None,
        expected_key_id=None,
        expected_artifact_id=artifact,
        expected_manifest_digest=evidence.manifest_digest,
    )
    assert not package.signed
    assert payload["reviews"][0]["reviewer_subject"] == "separate-reviewer"
    assert payload["challenges"][0]["status"] == "inconclusive"


def test_review_export_rechecks_revoked_or_missing_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = insufficient_manifest()
    artifact = uuid4()
    monkeypatch.setattr(
        "orca.application.review_export.authorize_project",
        lambda *_args: (_ for _ in ()).throw(LookupError("outside project")),
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def mappings(self):
            return self

        def one_or_none(self):
            return self.value

        def scalar_one_or_none(self):
            return self.value

    class Session:
        def execute(self, statement, params):
            if "FROM research.run_artifacts a" in str(statement):
                return Result({"project_id": evidence.project_id})
            assert "artifact_review_grants" in str(statement)
            assert params["subject"] == "outside-reviewer"
            return Result(None)

    with pytest.raises(LookupError, match="not found"):
        assemble_review_export(
            Session(),
            evidence.tenant_id,
            artifact,
            Principal("outside-reviewer", "test", frozenset({"evidence:read"})),
        )
