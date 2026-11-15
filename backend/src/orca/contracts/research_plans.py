"""Purpose: define bounded, typed plans for durable research investigations.
Behavior: accepts only allowlisted DAG operations and explicit USD cost limits.
Refs: main_implementation plan.md P05.1; api design.md §§3.6, 5.1; Phase: P05.
"""

from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator

from orca.contracts.common import Contract, MoneyBudget

PlanOperation = Literal[
    "source_metadata",
    "compatibility_check",
    "scientific_method",
    "evidence_manifest",
]
PlanNodeID = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")]
PlanAssumption = Annotated[str, Field(min_length=1, max_length=500)]


class PlanRequest(Contract):
    """HTTP input used to request deterministic typed plan construction."""

    strategy: Literal["typed"]
    method_hint: str | None = Field(default=None, min_length=1, max_length=160)
    data_constraints: dict[str, str | bool | int | None] = Field(max_length=24)
    budget: MoneyBudget

    @field_validator("data_constraints")
    @classmethod
    def bound_constraint_values(
        cls, values: dict[str, str | bool | int | None]
    ) -> dict[str, str | bool | int | None]:
        if any(not key or len(key) > 100 for key in values):
            raise ValueError("data constraint keys must contain 1 to 100 characters")
        if any(isinstance(value, str) and len(value) > 500 for value in values.values()):
            raise ValueError("data constraint text must not exceed 500 characters")
        if any(
            type(value) is int and abs(value) > 9_007_199_254_740_991 for value in values.values()
        ):
            raise ValueError("data constraint integers must be within the exact JSON range")
        return values


class PlanNode(Contract):
    """One allowlisted operation in a plan DAG; no user-supplied executable text."""

    node_id: PlanNodeID
    operation: PlanOperation
    depends_on: list[PlanNodeID] = Field(max_length=16)
    source_ids: list[UUID] = Field(default_factory=list, max_length=8)


class TypedPlan(Contract):
    """A versioned DAG whose digest binds every execution-relevant input."""

    schema_version: Literal["orca.research_plan/1"] = "orca.research_plan/1"
    revision: int = Field(ge=1, le=2_147_483_647)
    method_hint: str | None = Field(default=None, min_length=1, max_length=160)
    data_constraints: dict[str, str | bool | int | None] = Field(max_length=24)
    budget: MoneyBudget
    nodes: list[PlanNode] = Field(min_length=1, max_length=16)
    assumptions: list[PlanAssumption] = Field(max_length=24)
    source_ids: list[UUID] = Field(max_length=8)

    @field_validator("data_constraints")
    @classmethod
    def bound_constraint_values(
        cls, values: dict[str, str | bool | int | None]
    ) -> dict[str, str | bool | int | None]:
        if any(not key or len(key) > 100 for key in values):
            raise ValueError("data constraint keys must contain 1 to 100 characters")
        if any(isinstance(value, str) and len(value) > 500 for value in values.values()):
            raise ValueError("data constraint text must not exceed 500 characters")
        if any(
            type(value) is int and abs(value) > 9_007_199_254_740_991 for value in values.values()
        ):
            raise ValueError("data constraint integers must be within the exact JSON range")
        return values


def validate_plan(plan: TypedPlan, *, maximum_budget: Decimal) -> TypedPlan:
    """Validate graph shape and an explicit policy ceiling; do not infer scientific approval."""
    if maximum_budget < 0:
        raise ValueError("maximum budget policy must be nonnegative")
    if Decimal(plan.budget.max_amount) > maximum_budget:
        raise ValueError("plan budget exceeds the configured policy limit")

    node_ids = [node.node_id for node in plan.nodes]
    if len(set(node_ids)) != len(node_ids):
        raise ValueError("plan node identifiers must be unique")
    known = set(node_ids)
    by_id = {node.node_id: node for node in plan.nodes}
    if len(set(plan.source_ids)) != len(plan.source_ids):
        raise ValueError("plan source identifiers must be unique")

    for node in plan.nodes:
        if len(set(node.depends_on)) != len(node.depends_on):
            raise ValueError("node dependencies must be unique")
        if node.node_id in node.depends_on:
            raise ValueError("a plan node cannot depend on itself")
        if not set(node.depends_on) <= known:
            raise ValueError("plan dependency references an unknown node")
        if len(set(node.source_ids)) != len(node.source_ids):
            raise ValueError("node source identifiers must be unique")
        if not set(node.source_ids) <= set(plan.source_ids):
            raise ValueError("node source identifiers must be declared by the plan")

    # Iterative depth-first traversal avoids recursion limits even for malformed graphs.
    visiting: set[str] = set()
    visited: set[str] = set()
    for root in node_ids:
        if root in visited:
            continue
        stack: list[tuple[str, bool]] = [(root, False)]
        while stack:
            current, leaving = stack.pop()
            if leaving:
                visiting.remove(current)
                visited.add(current)
                continue
            if current in visiting:
                raise ValueError("plan graph contains a dependency cycle")
            if current in visited:
                continue
            visiting.add(current)
            stack.append((current, True))
            stack.extend((dependency, False) for dependency in by_id[current].depends_on)
    return plan
