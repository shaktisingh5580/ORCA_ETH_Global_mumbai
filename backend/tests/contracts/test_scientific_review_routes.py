"""Purpose: check P07 route surface and reviewer-grant boundaries without a live database.
Behavior: test scope, expiry and schema negatives; durable database acceptance remains separate.
Refs: main_implementation plan.md P07.2; Phase: P07.
"""

import base64
import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from fastapi.responses import Response
from pydantic import SecretStr, ValidationError

from orca.api.app import create_app
from orca.api.routers import evidence, reviews
from orca.application.review_export import ReviewExportPackage
from orca.config import Settings
from orca.contracts.scientific_reviews import (
    ChallengeRequest,
    ChallengeView,
    ResolveChallengeRequest,
)
from orca.domain.identity import Principal


class GrantSession:
    def __init__(self, expires_at: datetime, revoked_at: datetime | None = None) -> None:
        self.grant = {"expires_at": expires_at, "revoked_at": revoked_at}

    def execute(self, _statement: object, _params: object) -> SimpleNamespace:
        return SimpleNamespace(mappings=lambda: SimpleNamespace(one_or_none=lambda: self.grant))


def test_p07_review_routes_are_registered() -> None:
    app = create_app(
        Settings(monitoring_token=SecretStr(secrets.token_urlsafe(48)), environment="test")
    )
    document = app.openapi()
    assert "post" in document["paths"]["/v1/research/artifacts/{artifact_id}/challenges"]
    assert "post" in document["paths"]["/v1/research/artifacts/{artifact_id}/reviews"]
    assert "post" in document["paths"]["/v1/research/artifacts/{artifact_id}/review-grants"]
    assert "post" in document["paths"]["/v1/research/challenges/{challenge_id}/resolve"]
    alternate = document["paths"]["/v1/research/artifacts/{artifact_id}/reviews"]["get"]
    assert "application/vnd.orca.review+json" in alternate["responses"]["200"]["content"]


def test_portable_review_read_uses_same_principal_and_private_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact, tenant = uuid4(), uuid4()
    principal = Principal("reader", "test", frozenset({"evidence:read"}))
    state = SimpleNamespace()

    class UnitOfWork:
        def __init__(self, _engine: object, actual_tenant: object) -> None:
            assert actual_tenant == tenant

        def __enter__(self) -> object:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def assemble(
        _session: object,
        actual_tenant: object,
        actual_artifact: object,
        actual_principal: object,
        **kwargs: object,
    ) -> ReviewExportPackage:
        assert (actual_tenant, actual_artifact, actual_principal) == (tenant, artifact, principal)
        assert kwargs["signing_key"] is getattr(state, "review_export_signing_key", None)
        assert kwargs["signer_key_id"] == getattr(state, "review_export_key_id", None)
        signed = kwargs["signing_key"] is not None
        return ReviewExportPackage(b'{"payload":"test"}', "sha256:" + "a" * 64, signed)

    monkeypatch.setattr(reviews, "_review_tenant", lambda *_args: tenant)
    monkeypatch.setattr(reviews, "engine", lambda *_args: object())
    monkeypatch.setattr(reviews, "TenantUnitOfWork", UnitOfWork)
    monkeypatch.setattr(reviews, "assemble_review_export", assemble)
    response = reviews.list_reviews(
        artifact,
        SimpleNamespace(app=SimpleNamespace(state=state)),
        principal,
        accept="application/vnd.orca.review+json",
    )
    assert isinstance(response, Response)
    assert response.body == b'{"payload":"test"}'
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-orca-package-signature"] == "unsigned"
    assert response.headers["etag"] == '"sha256:' + "a" * 64 + '"'
    state.review_export_signing_key = Ed25519PrivateKey.generate()
    state.review_export_key_id = "operator"
    signed_response = reviews.list_reviews(
        artifact,
        SimpleNamespace(app=SimpleNamespace(state=state)),
        principal,
        accept="application/vnd.orca.review+json",
    )
    assert isinstance(signed_response, Response)
    assert signed_response.headers["x-orca-package-signature"] == "signed"


def test_review_package_signing_requires_valid_configured_key() -> None:
    token = SecretStr(secrets.token_urlsafe(48))
    with pytest.raises(ValidationError, match="configured together"):
        Settings(monitoring_token=token, environment="test", review_export_key_id="operator")
    with pytest.raises(ValidationError, match="32 bytes"):
        Settings(
            monitoring_token=token,
            environment="test",
            review_export_key_id="operator",
            review_export_signing_key=SecretStr("YQ"),
        )
    key = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
    app = create_app(
        Settings(
            monitoring_token=token,
            environment="test",
            review_export_key_id="operator",
            review_export_signing_key=SecretStr(key),
        )
    )
    assert app.state.review_export_key_id == "operator"
    assert app.state.review_export_signing_key is not None


def test_normal_review_list_remains_json_records(monkeypatch: pytest.MonkeyPatch) -> None:
    artifact, tenant, project = uuid4(), uuid4(), uuid4()
    principal = Principal("reader", "test", frozenset({"evidence:read"}))

    class UnitOfWork:
        def __init__(self, *_args: object) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, *_args: object):
            return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: []))

    monkeypatch.setattr(reviews, "_review_tenant", lambda *_args: tenant)
    monkeypatch.setattr(reviews, "engine", lambda *_args: object())
    monkeypatch.setattr(reviews, "TenantUnitOfWork", UnitOfWork)
    monkeypatch.setattr(reviews, "_original", lambda *_args: ({"project_id": project}, None))
    monkeypatch.setattr(reviews, "_review_access", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        reviews,
        "assemble_review_export",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected export")),
    )
    assert (
        reviews.list_reviews(artifact, SimpleNamespace(), principal, accept="application/json")
        == []
    )


@pytest.mark.parametrize("revoked,expired", [(True, False), (False, True)])
def test_reviewer_grant_is_rechecked(
    monkeypatch: pytest.MonkeyPatch, revoked: bool, expired: bool
) -> None:
    def no_project_access(*_args: object) -> None:
        raise LookupError("external reviewer")

    monkeypatch.setattr(reviews, "authorize_project", no_project_access)
    now = datetime.now(timezone.utc)
    session = GrantSession(
        now - timedelta(seconds=1) if expired else now + timedelta(hours=1),
        now if revoked else None,
    )
    principal = Principal("reviewer", "test", frozenset({"review:write"}))
    with pytest.raises(HTTPException) as caught:
        reviews._review_access(session, uuid4(), uuid4(), uuid4(), principal, write=True)
    assert caught.value.status_code == 404


def test_review_scope_required_even_with_active_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        reviews, "authorize_project", lambda *_args: (_ for _ in ()).throw(LookupError())
    )
    session = GrantSession(datetime.now(timezone.utc) + timedelta(hours=1))
    principal = Principal("reviewer", "test", frozenset())
    with pytest.raises(HTTPException) as caught:
        reviews._review_access(session, uuid4(), uuid4(), uuid4(), principal, write=True)
    assert caught.value.status_code == 403


def test_challenge_references_are_unique() -> None:
    source = uuid4()
    with pytest.raises(ValidationError, match="unique"):
        ChallengeRequest(
            discrepancy_type="wrong_baseline",
            source_refs=[source, source],
            statement="Wrong baseline year",
        )


def test_resolution_requires_distinct_evidence_for_conclusive_verdict() -> None:
    source = uuid4()
    with pytest.raises(ValidationError, match="immutable evidence reference"):
        ResolveChallengeRequest(verdict="rejected", limitations="No verified reference")
    with pytest.raises(ValidationError, match="unique"):
        ResolveChallengeRequest(
            verdict="upheld",
            arbitration_refs=[source, source],
            limitations="Duplicate evidence cannot establish the resolution",
        )
    assert (
        ResolveChallengeRequest(
            verdict="inconclusive", limitations="Evidence is insufficient to adjudicate"
        ).arbitration_refs
        == []
    )


def test_challenge_etag_binds_exact_immutable_record() -> None:
    identifier = uuid4()
    view = ChallengeView(
        id=identifier,
        artifact_id=uuid4(),
        reviewer_subject="reviewer",
        discrepancy_type="wrong_baseline",
        source_refs=[uuid4()],
        statement="The baseline does not match the claim",
        created_at=datetime.now(timezone.utc),
    )
    assert view.model_dump()["etag"] == f'"{identifier}:0"'
    assert view.model_copy(update={"response_count": 1}).etag == f'"{identifier}:1"'


@pytest.mark.parametrize("actor", ["author", "challenger", "responder"])
def test_resolution_rejects_self_adjudication(actor: str) -> None:
    with pytest.raises(HTTPException) as caught:
        reviews._require_separate_resolver(actor, "author", "challenger", {"responder"})
    assert caught.value.status_code == 403
    reviews._require_separate_resolver(
        "independent-reviewer", "author", "challenger", {"responder"}
    )


def test_external_reviewer_artifact_read_requires_scope_and_active_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        evidence, "authorize_project", lambda *_args: (_ for _ in ()).throw(LookupError())
    )

    class ReadSession:
        def __init__(self, grant: int | None) -> None:
            self.grant = grant

        def execute(self, _statement: object, _params: object) -> SimpleNamespace:
            return SimpleNamespace(scalar_one_or_none=lambda: self.grant)

    identifiers = (uuid4(), uuid4(), uuid4())
    reader = Principal("reviewer", "test", frozenset({"evidence:read"}))
    evidence._authorize_artifact_read(ReadSession(1), *identifiers, reader)
    with pytest.raises(HTTPException) as missing:
        evidence._authorize_artifact_read(ReadSession(None), *identifiers, reader)
    assert missing.value.status_code == 404
    with pytest.raises(HTTPException) as no_scope:
        evidence._authorize_artifact_read(
            ReadSession(1), *identifiers, Principal("reviewer", "test", frozenset())
        )
    assert no_scope.value.status_code == 403
