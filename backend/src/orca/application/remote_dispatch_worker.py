"""Purpose: execute P09 queued research delegations under durable lease fences.
Behavior: persist one signed grant, recheck current authority around A2A, and retain verified replies.
Refs: main_implementation plan.md P09.2-P09.3; api design.md §§2.2,17.1; Phase: P09.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.application.federation import (
    ResearchDispatchScope,
    ResearchGrant,
    SignedResearchGrant,
    sign_grant,
)
from orca.application.workspace_access import authorize_project
from orca.domain.identity import Principal
from orca.infrastructure.federation.coordinator import FederationCoordinator
from orca.infrastructure.federation.research_a2a_client import (
    cancel_research_task,
    dispatch_research_task,
    verified_artifact,
)
from orca.infrastructure.federation.signing import digest

Exchange = Callable[
    [SignedResearchGrant, dict[str, Any], ResearchDispatchScope], Awaitable[dict[str, Any]]
]


@dataclass(frozen=True, slots=True)
class LeasedDispatch:
    id: UUID
    run_id: UUID
    token: UUID
    generation: int
    signed: SignedResearchGrant | None


class RemoteDispatchWorker:
    """One worker instance owns no durable state outside PostgreSQL."""

    def __init__(
        self,
        tenant: UUID,
        project: UUID,
        transaction: Callable[[], AbstractContextManager[Session]],
        coordinator: FederationCoordinator,
        exchange: Exchange | None = None,
    ) -> None:
        self.tenant, self.project = tenant, project
        self.transaction, self.coordinator = transaction, coordinator
        self.exchange = exchange

    async def _exchange(
        self,
        signed: SignedResearchGrant,
        agent: dict[str, Any],
        scope: ResearchDispatchScope,
        checkpoint: Callable[[str, str], None],
    ) -> dict[str, Any]:
        return await dispatch_research_task(
            signed,
            agent,
            scope,
            self.coordinator.id,
            self.coordinator.public_key,
            self.coordinator.private_key,
            self.coordinator.key_id,
            checkpoint=checkpoint,
        )

    def _checkpoint(self, lease: LeasedDispatch, task_id: str, context_id: str) -> None:
        with self.transaction() as session:
            session.execute(
                text("""UPDATE federation.research_dispatches SET remote_task_id=:task,
                  remote_context_id=:context,updated_at=clock_timestamp()
                  WHERE tenant_id=:tenant AND project_id=:project AND id=:id
                    AND remote_task_id IS NULL AND (
                      (state='leased' AND generation=:generation AND lease_token=:token
                        AND lease_until>clock_timestamp())
                      OR (state='cancelled' AND generation=:generation+1))"""),
                {
                    **self._params(lease.id),
                    "task": task_id,
                    "context": context_id,
                    "generation": lease.generation,
                    "token": lease.token,
                },
            )

    def _cancel_once(self) -> bool:
        with self.transaction() as session:
            row = (
                session.execute(
                    text("""WITH candidate AS (
                  SELECT d.id,v.registration FROM federation.research_dispatches d
                  JOIN federation.registration_versions v ON v.tenant_id=d.tenant_id
                    AND v.id=d.binding_version AND v.agent_id=d.agent_id
                  WHERE d.tenant_id=:tenant AND d.project_id=:project AND d.state='cancelled'
                    AND d.remote_task_id IS NOT NULL AND d.signed_grant IS NOT NULL
                    AND d.remote_cancel_attempted_at IS NULL
                  ORDER BY d.updated_at,d.id LIMIT 1 FOR UPDATE OF d SKIP LOCKED)
                  UPDATE federation.research_dispatches d
                  SET remote_cancel_attempted_at=clock_timestamp(),updated_at=clock_timestamp()
                  FROM candidate c WHERE d.id=c.id
                  RETURNING d.id,d.agent_id,d.binding_version,d.generation,d.signed_grant,
                    d.grant_digest,d.remote_task_id,d.remote_context_id,c.registration"""),
                    {"tenant": self.tenant, "project": self.project},
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return False
        signed = SignedResearchGrant.model_validate(row["signed_grant"])
        if (
            signed.grant.task_id != row["id"]
            or signed.grant.project_id != self.project
            or signed.grant.audience_agent_id != row["agent_id"]
            or signed.grant.binding_version != row["binding_version"]
            or signed.grant_digest != row["grant_digest"]
        ):
            return True

        async def cancel() -> bool:
            async with asyncio.timeout(90):
                return await cancel_research_task(
                    signed,
                    row["registration"],
                    self.coordinator,
                    (row["remote_task_id"], row["remote_context_id"]),
                )

        try:
            confirmed = asyncio.run(cancel())
        except Exception:
            # An attempted request may have reached the provider. Preserve the unknown outcome.
            return True
        if confirmed:
            with self.transaction() as session:
                session.execute(
                    text("""UPDATE federation.research_dispatches
                      SET remote_cancel_confirmed_at=clock_timestamp(),error_code=NULL,
                        updated_at=clock_timestamp()
                      WHERE tenant_id=:tenant AND project_id=:project AND id=:id
                        AND state='cancelled' AND generation=:generation"""),
                    {**self._params(row["id"]), "generation": row["generation"]},
                )
        return True

    def _params(self, identifier: UUID) -> dict[str, UUID]:
        return {"tenant": self.tenant, "project": self.project, "id": identifier}

    def _lock_project(self, session: Session) -> None:
        # Match the lock order used by authorize_project in the dispatch API.
        session.execute(
            text(
                "SELECT id FROM research.projects WHERE tenant_id=:tenant AND id=:project FOR UPDATE"
            ),
            {"tenant": self.tenant, "project": self.project},
        )

    def _current_context(
        self, session: Session, row: dict[str, Any]
    ) -> tuple[ResearchDispatchScope, dict[str, Any]]:
        """Re-read every revocable fact; do not rely on the enqueue snapshot."""
        params = self._params(row["id"])
        actor = Principal(row["created_by"], "remote-worker", frozenset({"research:run"}))
        authorize_project(session, self.tenant, self.project, actor, "research:run")
        run = (
            session.execute(
                text("""SELECT r.state,r.plan_digest,b.state AS reservation_state,b.amount
              FROM research.compute_runs r JOIN research.run_budget_reservations b
                ON b.tenant_id=r.tenant_id AND b.project_id=r.project_id AND b.run_id=r.id
              WHERE r.tenant_id=:tenant AND r.project_id=:project AND r.id=:run
              FOR SHARE OF r,b"""),
                {**params, "run": row["run_id"]},
            )
            .mappings()
            .one_or_none()
        )
        if (
            run is None
            or run["state"] not in {"QUEUED", "RUNNING"}
            or run["reservation_state"] != "reserved"
            or run["plan_digest"] != row["plan_digest"]
            or row["budget_usd"] > run["amount"]
        ):
            raise PermissionError("Approved run or reservation changed")
        agent = (
            session.execute(
                text("""SELECT r.id,r.organization_id,r.state,r.binding_version,v.registration,
              (SELECT max(q.expires_at) FROM federation.agent_qualifications q
               WHERE q.tenant_id=r.tenant_id AND q.agent_id=r.id
                 AND q.binding_version=r.binding_version AND q.report->>'status'='PASS')
                 AS qualification_expires_at
              FROM federation.registrations r JOIN federation.registration_versions v
                ON v.tenant_id=r.tenant_id AND v.id=r.binding_version
              WHERE r.tenant_id=:tenant AND r.organization_id=:organization AND r.id=:agent
              FOR SHARE OF r"""),
                {**params, "organization": row["organization_id"], "agent": row["agent_id"]},
            )
            .mappings()
            .one_or_none()
        )
        now = datetime.now(timezone.utc)
        if (
            agent is None
            or agent["state"] != "active"
            or agent["binding_version"] != row["binding_version"]
            or agent["registration"].get("capability") != row["capability_id"]
            or agent["qualification_expires_at"] is None
            or agent["qualification_expires_at"] <= now
        ):
            raise PermissionError("Current agent binding is not qualified")
        authorized = session.execute(
            text("""SELECT 1 FROM federation.research_authorizations
              WHERE id=:authorization AND tenant_id=:tenant AND project_id=:project
                AND organization_id=:organization AND agent_id=:agent
                AND binding_version=:binding AND capability_id=:capability
                AND method_id=:method AND revoked_at IS NULL AND expires_at>clock_timestamp()"""),
            {
                **params,
                "authorization": row["authorization_id"],
                "organization": row["organization_id"],
                "agent": row["agent_id"],
                "binding": row["binding_version"],
                "capability": row["capability_id"],
                "method": row["method_id"],
            },
        ).scalar_one_or_none()
        if authorized is None:
            raise PermissionError("Research authorization was revoked or expired")
        datasets = list(row["dataset_ids"])
        if not datasets or len(set(datasets)) != len(datasets):
            raise PermissionError("Remote dataset scope changed")
        for source in datasets:
            granted = session.execute(
                text("""SELECT 1 FROM research.snapshot_project_grants
                  WHERE tenant_id=:tenant AND project_id=:project AND snapshot_id=:source
                    AND (expires_at IS NULL OR expires_at>clock_timestamp())"""),
                {**params, "source": source},
            ).scalar_one_or_none()
            if granted is None:
                raise PermissionError("Remote source grant expired")
        scope = ResearchDispatchScope(
            agent_id=row["agent_id"],
            binding_version=row["binding_version"],
            organization_id=row["organization_id"],
            project_id=self.project,
            run_id=row["run_id"],
            task_id=row["id"],
            capability_id=row["capability_id"],
            approved_plan_digest=row["plan_digest"],
            approved_method_id=row["method_id"],
            permitted_dataset_ids=set(datasets),
            available_budget_usd=row["budget_usd"],
            service_eligible=True,
            research_dispatch_enabled=True,
        )
        return scope, {**dict(agent), "research_dispatch_enabled": True}

    def _row(
        self, session: Session, identifier: UUID, *, lock: bool = False
    ) -> dict[str, Any] | None:
        row = (
            session.execute(
                text(
                    """SELECT * FROM federation.research_dispatches
              WHERE tenant_id=:tenant AND project_id=:project AND id=:id"""
                    + (" FOR UPDATE" if lock else "")
                ),
                self._params(identifier),
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None

    def _finish(
        self,
        session: Session,
        lease: LeasedDispatch,
        state: str,
        code: str,
        response: dict[str, Any] | None = None,
    ) -> bool:
        result_digest = response["artifact_digest"] if response is not None else None
        return (
            session.execute(
                text("""UPDATE federation.research_dispatches
              SET state=:state,lease_token=NULL,lease_until=NULL,
                verified_response=CAST(:response AS jsonb),result_digest=:digest,
                error_code=:code,updated_at=clock_timestamp()
              WHERE tenant_id=:tenant AND project_id=:project AND id=:id AND state='leased'
                AND lease_token=:token AND generation=:generation
                AND lease_until>clock_timestamp() RETURNING id"""),
                {
                    **self._params(lease.id),
                    "token": lease.token,
                    "generation": lease.generation,
                    "state": state,
                    "code": code,
                    "response": json.dumps(response) if response is not None else None,
                    "digest": result_digest,
                },
            ).scalar_one_or_none()
            is not None
        )

    def _claim(self, session: Session) -> LeasedDispatch | None:
        row = (
            session.execute(
                text("""SELECT * FROM federation.research_dispatches
              WHERE tenant_id=:tenant AND project_id=:project AND state='queued'
              ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1"""),
                {"tenant": self.tenant, "project": self.project},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        item = dict(row)
        token, generation = uuid4(), item["generation"] + 1
        session.execute(
            text("""UPDATE federation.research_dispatches SET state='leased',generation=:generation,
              lease_token=:token,lease_until=clock_timestamp()+interval '120 seconds',
              updated_at=clock_timestamp() WHERE tenant_id=:tenant AND project_id=:project AND id=:id"""),
            {**self._params(item["id"]), "generation": generation, "token": token},
        )
        lease = LeasedDispatch(item["id"], item["run_id"], token, generation, None)
        try:
            self._current_context(session, item)
        except (LookupError, PermissionError, ValueError):
            self._finish(session, lease, "failed", "AUTHORIZATION_CHANGED")
            return None
        grant = ResearchGrant(
            audience_agent_id=item["agent_id"],
            binding_version=item["binding_version"],
            organization_id=item["organization_id"],
            project_id=self.project,
            run_id=item["run_id"],
            task_id=item["id"],
            capability_id=item["capability_id"],
            approved_plan_digest=item["plan_digest"],
            method_id=item["method_id"],
            dataset_ids=list(item["dataset_ids"]),
            max_cost_usd=item["budget_usd"],
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=3),
        )
        signed = sign_grant(grant, self.coordinator.private_key, self.coordinator.key_id)
        session.execute(
            text("""UPDATE federation.research_dispatches SET signed_grant=CAST(:signed AS jsonb),
              grant_digest=:digest WHERE tenant_id=:tenant AND project_id=:project AND id=:id"""),
            {
                **self._params(item["id"]),
                "signed": signed.model_dump_json(),
                "digest": signed.grant_digest,
            },
        )
        return LeasedDispatch(item["id"], item["run_id"], token, generation, signed)

    def run_once(
        self,
    ) -> Literal[
        "idle",
        "completed",
        "rejected",
        "reconciliation_required",
        "lease_lost",
        "remote_cancel_attempted",
    ]:
        """Never replay an expired remote lease: the provider may have completed it."""
        if self._cancel_once():
            return "remote_cancel_attempted"
        with self.transaction() as session:
            self._lock_project(session)
            expired = session.execute(
                text("""UPDATE federation.research_dispatches
                  SET state='reconciliation_required',lease_token=NULL,lease_until=NULL,
                    error_code='REMOTE_OUTCOME_UNKNOWN',updated_at=clock_timestamp()
                  WHERE tenant_id=:tenant AND project_id=:project AND state='leased'
                    AND lease_until<=clock_timestamp() RETURNING id"""),
                {"tenant": self.tenant, "project": self.project},
            ).first()
            lease = self._claim(session)
        if lease is None:
            return "reconciliation_required" if expired else "idle"
        assert lease.signed is not None
        with self.transaction() as session:
            self._lock_project(session)
            row = self._row(session, lease.id)
            if (
                row is None
                or row["state"] != "leased"
                or row["lease_token"] != lease.token
                or row["generation"] != lease.generation
                or row["lease_until"] <= datetime.now(timezone.utc)
            ):
                return "lease_lost"
            try:
                scope, agent = self._current_context(session, row)
            except (LookupError, PermissionError, ValueError):
                if self._finish(session, lease, "failed", "AUTHORIZATION_CHANGED"):
                    return "rejected"
                return "lease_lost"
        try:

            async def exchange_once() -> dict[str, Any]:
                assert lease.signed is not None
                async with asyncio.timeout(90):
                    if self.exchange is not None:
                        return await self.exchange(lease.signed, agent, scope)
                    return await self._exchange(
                        lease.signed,
                        agent,
                        scope,
                        lambda task, context: self._checkpoint(lease, task, context),
                    )

            result = asyncio.run(exchange_once())
        except Exception:
            with self.transaction() as session:
                if self._finish(
                    session, lease, "reconciliation_required", "REMOTE_OUTCOME_UNKNOWN"
                ):
                    return "reconciliation_required"
            return "lease_lost"
        with self.transaction() as session:
            self._lock_project(session)
            row = self._row(session, lease.id, lock=True)
            if row is None:
                return "lease_lost"
            try:
                _, current_agent = self._current_context(session, row)
                if row["grant_digest"] != lease.signed.grant_digest or row[
                    "signed_grant"
                ] != lease.signed.model_dump(mode="json"):
                    raise ValueError("Persisted grant changed")
                envelope = result["signed_artifact"]
                candidate = {
                    "result": {
                        "kind": "task",
                        "id": result["a2a_task_id"],
                        "contextId": result["a2a_context_id"],
                        "status": {"state": "completed"},
                        "artifacts": [{"parts": [{"kind": "data", "data": envelope}]}],
                    }
                }
                checked = verified_artifact(candidate, lease.signed, current_agent["registration"])
                if checked != result or result["artifact_digest"] != digest(result["artifact"]):
                    raise ValueError("Verified remote artifact changed")
            except (KeyError, LookupError, PermissionError, ValueError):
                if self._finish(session, lease, "failed", "AUTHORITY_OR_ARTIFACT_CHANGED"):
                    return "rejected"
                return "lease_lost"
            if self._finish(session, lease, "completed", "", result):
                return "completed"
            return "lease_lost"
