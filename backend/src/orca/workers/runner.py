"""Purpose: execute bounded P05 jobs with durable fencing and truthful blocked outcomes.
Behavior: claims one PostgreSQL job, checks finite plan limits, and never invents science when authority is absent.
Refs: main_implementation plan.md P05.3; api design.md §5.1; Phase: P05.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from orca.infrastructure.jobs.postgres_queue import ClaimedJob, PostgresQueue


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """Fixed limits for P05's first worker; no user payload can raise them."""

    max_plan_nodes: int = 16
    max_tool_calls: int = 8
    max_attempts: int = 3


def classify_failure(error: Exception) -> Literal["retry", "inconclusive"]:
    """Retry only explicitly transient infrastructure failures, never validation/authority failures."""
    return "retry" if isinstance(error, TimeoutError) else "inconclusive"


class RunWorker:
    """One-at-a-time queue worker; external retrieval and scientific publication are deferred to later handlers."""

    def __init__(
        self,
        tenant: UUID,
        project: UUID,
        transaction: Callable[[], AbstractContextManager[Session]],
    ):
        self.tenant, self.project, self.transaction = tenant, project, transaction

    def run_once(
        self, policy: ExecutionPolicy = ExecutionPolicy()
    ) -> Literal["idle", "inconclusive", "lease_lost"]:
        """Claim and durably close a run that cannot yet satisfy real science prerequisites."""
        with self.transaction() as session:
            queue = PostgresQueue(session, self.tenant, self.project)
            claimed = queue.claim()
            if claimed is None:
                return "idle"
            reason = self._blocked_reason(claimed, policy)
            if queue.finish_inconclusive(claimed, reason):
                return "inconclusive"
            return "lease_lost"

    @staticmethod
    def _blocked_reason(claimed: ClaimedJob, policy: ExecutionPolicy) -> str:
        """Check only bounded orchestration metadata; actual science handlers remain unavailable."""
        nodes = claimed.payload.get("node_count", 0)
        tools = claimed.payload.get("tool_budget", 0)
        if not isinstance(nodes, int) or nodes < 0 or nodes > policy.max_plan_nodes:
            return "plan_node_limit"
        if not isinstance(tools, int) or tools < 0 or tools > policy.max_tool_calls:
            return "tool_budget_limit"
        return "source_or_approved_method_unavailable"
