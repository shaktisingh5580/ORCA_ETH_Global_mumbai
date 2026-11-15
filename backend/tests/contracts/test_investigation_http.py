"""Purpose: verify P05 HTTP registration, planner boundaries and approval authorization.
Behavior: prove the OpenAPI surface exposes required guards without a database fixture.
Refs: main_implementation plan.md P05.1; api design.md §3.6; Phase: P05.
"""

import secrets
from decimal import Decimal

from pydantic import SecretStr

from orca.api.app import create_app
from orca.application.planning import compile_typed_plan, plan_digest
from orca.config import Settings
from orca.contracts.investigations import ApprovePlan
from orca.contracts.research_plans import PlanRequest
from orca.domain.identity import ROLE_SCOPES, Principal


def request() -> PlanRequest:
    return PlanRequest(
        strategy="typed",
        method_hint="sst.seasonal_anomaly",
        data_constraints={"require_pinned_product_version": True},
        budget={"max_amount": "2.00"},
    )


def test_typed_plan_compilation_is_deterministic_and_declares_unverified_readiness() -> None:
    first, second = (
        compile_typed_plan(request(), revision=1),
        compile_typed_plan(request(), revision=1),
    )
    assert first == second
    assert [node.operation for node in first.nodes] == [
        "source_metadata",
        "compatibility_check",
        "scientific_method",
        "evidence_manifest",
    ]
    assert first.source_ids == []
    assert "approved by planning" in first.assumptions[0]
    assert plan_digest(first, maximum_budget=Decimal("100")) == plan_digest(
        second, maximum_budget=Decimal("100")
    )


def test_approval_contract_rejects_duplicate_assumptions() -> None:
    digest = "sha256:" + "0" * 64
    try:
        ApprovePlan(digest=digest, accepted_assumptions=["a", "a"])
    except ValueError as error:
        assert "unique" in str(error)
    else:
        raise AssertionError("duplicate acknowledgement must be rejected")


def test_approval_requires_research_run_role() -> None:
    all_scopes = frozenset().union(*ROLE_SCOPES.values())
    researcher = Principal("researcher", "test", all_scopes)
    investigator = Principal("investigator", "test", all_scopes)
    assert "research:run" not in researcher.effective_scopes({"researcher"})
    assert "research:run" in investigator.effective_scopes({"principal_investigator"})


def test_investigation_openapi_includes_p05_guards_and_models() -> None:
    app = create_app(
        Settings(monitoring_token=SecretStr(secrets.token_urlsafe(48)), environment="test")
    )
    paths = app.openapi()["paths"]
    base = "/v1/research/investigations"
    approval = base + "/{investigation_id}/plans/{plan_id}/approve"
    assert {"get", "post"} <= set(paths[base])
    assert paths[base + "/{investigation_id}/plans"]["post"]["responses"].get("202")
    parameters = {item["name"].lower(): item for item in paths[approval]["post"]["parameters"]}
    assert parameters["idempotency-key"]["required"] is True
    assert parameters["if-match"]["required"] is False
    assert "ApprovePlan" in app.openapi()["components"]["schemas"]
