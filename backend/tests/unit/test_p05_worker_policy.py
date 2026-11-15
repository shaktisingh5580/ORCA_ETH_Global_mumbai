"""Purpose: protect P05 worker limits and retry classification from unsafe broadening.
Behavior: verifies only timeout failures are retryable and payload limits become explicit blocked outcomes.
Refs: main_implementation plan.md P05.3; Phase: P05.
"""

from uuid import uuid4

from orca.infrastructure.jobs.postgres_queue import ClaimedJob
from orca.workers.runner import ExecutionPolicy, RunWorker, classify_failure


def claim(payload: dict[str, object]) -> ClaimedJob:
    return ClaimedJob(uuid4(), uuid4(), uuid4(), uuid4(), 1, payload)


def test_retry_only_safe_errors() -> None:
    assert classify_failure(TimeoutError()) == "retry"
    assert classify_failure(ValueError("invalid")) == "inconclusive"


def test_max_depth_and_tool_budget() -> None:
    policy = ExecutionPolicy(max_plan_nodes=2, max_tool_calls=1)
    assert RunWorker._blocked_reason(claim({"node_count": 3}), policy) == "plan_node_limit"
    assert (
        RunWorker._blocked_reason(claim({"node_count": 2, "tool_budget": 2}), policy)
        == "tool_budget_limit"
    )
