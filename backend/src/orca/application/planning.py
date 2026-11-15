"""Purpose: validate and fingerprint durable research plans without executing them.
Behavior: applies explicit budget policy and computes a canonical, domain-separated digest.
Refs: main_implementation plan.md P05.1; api design.md §§3.6, 5.1; Phase: P05.
"""

import hashlib
from decimal import Decimal

import rfc8785

from orca.contracts.research_plans import TypedPlan, validate_plan


def plan_digest(plan: TypedPlan, *, maximum_budget: Decimal) -> str:
    """Return a stable digest over the validated plan's complete JSON representation."""
    validated = validate_plan(plan, maximum_budget=maximum_budget)
    canonical = rfc8785.dumps(validated.model_dump(mode="json"))
    return "sha256:" + hashlib.sha256(b"orca.research-plan.v1\0" + canonical).hexdigest()
