"""Purpose: verify P05 typed-plan graph, budget and canonical digest invariants.
Behavior: prove malformed plans fail closed and execution-relevant edits rebind approval.
Refs: main_implementation plan.md P05.1; api design.md §3.6; Phase: P05.
"""

from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from orca.application.planning import plan_digest
from orca.contracts.research_plans import PlanNode, TypedPlan

SOURCE = UUID("00000000-0000-4000-8000-000000000001")


def make_plan() -> TypedPlan:
    return TypedPlan(
        revision=1,
        method_hint="sst.seasonal_anomaly",
        data_constraints={"require_pinned_product_version": True},
        budget={"currency": "USD", "max_amount": "2.00"},
        source_ids=[SOURCE],
        assumptions=["source_baseline_matched"],
        nodes=[
            PlanNode(
                node_id="metadata", operation="source_metadata", depends_on=[], source_ids=[SOURCE]
            ),
            PlanNode(
                node_id="compatibility",
                operation="compatibility_check",
                depends_on=["metadata"],
                source_ids=[SOURCE],
            ),
        ],
    )


def test_invalid_graph_cycle_rejected() -> None:
    plan = make_plan()
    plan.nodes[0].depends_on.append("compatibility")
    with pytest.raises(ValueError, match="cycle"):
        plan_digest(plan, maximum_budget=Decimal("10.00"))


def test_plan_budget_rejected() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        plan_digest(make_plan(), maximum_budget=Decimal("1.99"))


def test_identical_validated_plan_has_stable_digest() -> None:
    assert plan_digest(make_plan(), maximum_budget=Decimal("2.00")) == plan_digest(
        make_plan(), maximum_budget=Decimal("2.00")
    )


def test_execution_relevant_edit_changes_digest() -> None:
    original = make_plan()
    changed = make_plan()
    changed.nodes[1].depends_on = []
    assert plan_digest(original, maximum_budget=Decimal("2.00")) != plan_digest(
        changed, maximum_budget=Decimal("2.00")
    )


def test_unknown_plan_operation_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PlanNode(node_id="custom", operation="run_shell", depends_on=[])  # type: ignore[arg-type]


def test_plan_constraints_are_bounded() -> None:
    payload = make_plan().model_dump()
    payload["data_constraints"] = {"source": "x" * 501}
    with pytest.raises(ValidationError, match="500 characters"):
        TypedPlan.model_validate(payload)


def test_plan_integer_constraints_fit_canonical_json_range() -> None:
    payload = make_plan().model_dump()
    payload["data_constraints"] = {"window": 9_007_199_254_740_992}
    with pytest.raises(ValidationError, match="exact JSON range"):
        TypedPlan.model_validate(payload)
