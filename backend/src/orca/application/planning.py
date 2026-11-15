"""Purpose: validate and fingerprint durable research plans without executing them.
Behavior: applies explicit budget policy and computes a canonical, domain-separated digest.
Refs: main_implementation plan.md P05.1; api design.md §§3.6, 5.1; Phase: P05.
"""

import hashlib
from decimal import Decimal

import rfc8785

from orca.contracts.research_plans import PlanNode, PlanRequest, TypedPlan, validate_plan

__all__ = ["compile_typed_plan", "plan_digest", "validate_plan"]


def compile_typed_plan(request: PlanRequest, *, revision: int) -> TypedPlan:
    """Compile the one bounded P05 planner shape; never execute user-authored code.

    Source selection and scientific-method approval are intentionally deferred. The
    resulting candidate is therefore usable for review and exact-digest approval,
    but does not claim verified source readiness or a scientific conclusion.
    """
    return TypedPlan(
        revision=revision,
        method_hint=request.method_hint,
        data_constraints=request.data_constraints,
        budget=request.budget,
        nodes=[
            PlanNode(node_id="source-metadata", operation="source_metadata", depends_on=[]),
            PlanNode(
                node_id="compatibility",
                operation="compatibility_check",
                depends_on=["source-metadata"],
            ),
            PlanNode(
                node_id="scientific-method",
                operation="scientific_method",
                depends_on=["compatibility"],
            ),
            PlanNode(
                node_id="evidence-manifest",
                operation="evidence_manifest",
                depends_on=["scientific-method"],
            ),
        ],
        assumptions=[
            "No source snapshot, compatibility decision, or scientific method is approved by planning alone."
        ],
        source_ids=[],
    )


def plan_digest(plan: TypedPlan, *, maximum_budget: Decimal) -> str:
    """Return a stable digest over the validated plan's complete JSON representation."""
    validated = validate_plan(plan, maximum_budget=maximum_budget)
    canonical = rfc8785.dumps(validated.model_dump(mode="json"))
    return "sha256:" + hashlib.sha256(b"orca.research-plan.v1\0" + canonical).hexdigest()
