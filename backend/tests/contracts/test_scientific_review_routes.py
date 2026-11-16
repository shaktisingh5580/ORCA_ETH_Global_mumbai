"""Purpose: check P07 route surface and reviewer-grant boundaries without a live database.
Behavior: test scope, expiry and schema negatives; durable database acceptance remains separate.
Refs: main_implementation plan.md P07.2; Phase: P07.
"""

import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError

from orca.api.app import create_app
from orca.api.routers import evidence, reviews
from orca.config import Settings
from orca.contracts.scientific_reviews import ChallengeRequest
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
