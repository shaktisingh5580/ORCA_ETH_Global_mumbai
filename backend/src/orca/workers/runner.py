"""Purpose: execute P05 jobs with durable stage checkpoints and fenced evidence publication.
Behavior: run an injected scientific handler outside DB transactions, then verify every write under a live lease.
Refs: main_implementation plan.md P05.3-P05.5; api design.md §§5.1, 6.2; Phase: P05.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.application.evidence import ScientificEvidence
from orca.contracts.research_plans import TypedPlan
from orca.contracts.science_evidence import ScienceEvidenceManifest
from orca.infrastructure.jobs.postgres_queue import ClaimedJob, PostgresQueue


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """Fixed limits for a worker; no user payload can raise them."""

    max_plan_nodes: int = 16
    max_tool_calls: int = 8
    max_attempts: int = 3


@dataclass(frozen=True, slots=True)
class StageOutput:
    node_id: str
    input_digest: str
    output_digest: str


@dataclass(frozen=True, slots=True)
class ScientificExecution:
    stages: tuple[StageOutput, ...]
    manifest: ScienceEvidenceManifest


@dataclass(frozen=True, slots=True)
class RunContext:
    actor: str
    created_at: datetime
    approved_digest: str


class ScientificHandler(Protocol):
    def prepare(
        self, claimed: ClaimedJob, plan: TypedPlan, context: RunContext
    ) -> ScientificExecution: ...


def classify_failure(error: Exception) -> Literal["retry", "inconclusive"]:
    """Retry only explicitly transient infrastructure failures, never validation failures."""
    return "retry" if isinstance(error, TimeoutError) else "inconclusive"


class RunWorker:
    """A handler may compute science, but only the fenced queue can publish it."""

    def __init__(
        self,
        tenant: UUID,
        project: UUID,
        transaction: Callable[[], AbstractContextManager[Session]],
        handler: ScientificHandler | None = None,
    ):
        self.tenant, self.project, self.transaction = tenant, project, transaction
        self.handler = handler

    @staticmethod
    def _blocked_reason(claimed: ClaimedJob, policy: ExecutionPolicy) -> str:
        nodes = claimed.payload.get("node_count", 0)
        tools = claimed.payload.get("tool_budget", 0)
        if not isinstance(nodes, int) or nodes < 0 or nodes > policy.max_plan_nodes:
            return "plan_node_limit"
        if not isinstance(tools, int) or tools < 0 or tools > policy.max_tool_calls:
            return "tool_budget_limit"
        if claimed.generation > policy.max_attempts:
            return "attempt_limit"
        return "source_or_approved_method_unavailable"

    def _close_blocked(
        self, claimed: ClaimedJob, reason: str
    ) -> Literal["inconclusive", "lease_lost"]:
        with self.transaction() as session:
            queue = PostgresQueue(session, self.tenant, self.project)
            return "inconclusive" if queue.finish_inconclusive(claimed, reason) else "lease_lost"

    def _plan(self, claimed: ClaimedJob) -> tuple[TypedPlan, RunContext] | None:
        with self.transaction() as session:
            row = (
                session.execute(
                    text("""SELECT p.plan,r.created_by,r.created_at,r.plan_digest FROM research.compute_runs r
                      JOIN research.investigation_plans p ON p.tenant_id=r.tenant_id
                        AND p.project_id=r.project_id AND p.id=r.plan_id
                      WHERE r.tenant_id=:tenant AND r.project_id=:project AND r.id=:run
                        AND r.state='RUNNING'"""),
                    {"tenant": self.tenant, "project": self.project, "run": claimed.run_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            return TypedPlan.model_validate(row["plan"]), RunContext(
                str(row["created_by"]), row["created_at"], str(row["plan_digest"])
            )

    def run_once(
        self, policy: ExecutionPolicy = ExecutionPolicy()
    ) -> Literal["idle", "inconclusive", "lease_lost", "published"]:
        """Claim, compute, checkpoint and atomically publish; a stale lease can never publish."""
        with self.transaction() as session:
            claimed = PostgresQueue(session, self.tenant, self.project).claim()
        if claimed is None:
            return "idle"
        reason = self._blocked_reason(claimed, policy)
        if self.handler is None or reason != "source_or_approved_method_unavailable":
            return self._close_blocked(claimed, reason)
        context = self._plan(claimed)
        if context is None:
            return "lease_lost"
        plan, run_context = context
        if len(plan.nodes) > policy.max_plan_nodes or len(plan.nodes) != claimed.payload.get(
            "node_count"
        ):
            return self._close_blocked(claimed, "plan_node_limit")
        try:
            execution = self.handler.prepare(claimed, plan, run_context)
        except TimeoutError:
            return self._close_blocked(claimed, "source_timeout")
        except (ValueError, LookupError, PermissionError):
            return self._close_blocked(claimed, "scientific_validation")
        if [stage.node_id for stage in execution.stages] != [node.node_id for node in plan.nodes]:
            return self._close_blocked(claimed, "stage_graph_mismatch")
        if (
            execution.manifest.tenant_id != self.tenant
            or execution.manifest.project_id != self.project
            or execution.manifest.created_by != run_context.actor
        ):
            return self._close_blocked(claimed, "evidence_authority_mismatch")
        for stage in execution.stages:
            with self.transaction() as session:
                queue = PostgresQueue(session, self.tenant, self.project)
                if not queue.renew(claimed):
                    return "lease_lost"
                if (
                    queue.checkpoint(
                        claimed, stage.node_id, stage.input_digest, stage.output_digest
                    )
                    is None
                ):
                    return "lease_lost"
        with self.transaction() as session:
            queue = PostgresQueue(session, self.tenant, self.project)
            if not queue.renew(claimed):
                return "lease_lost"
            manifest_id = ScientificEvidence(session, self.tenant, run_context.actor).persist(
                execution.manifest
            )
            artifact = queue.publish_evidence(claimed, manifest_id)
            return "published" if artifact is not None else "lease_lost"
