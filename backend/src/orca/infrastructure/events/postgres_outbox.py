"""Purpose: append and replay redacted durable P05 run events.
Behavior: allocates a per-run sequence under a row lock; never persists private prompts, tokens or raw source bytes.
Refs: main_implementation plan.md P05.4; api design.md §5.3; Phase: P05.
"""

import json
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class RunEvent:
    id: UUID
    sequence: int
    event_type: str
    payload: dict[str, object]


def redact_payload(payload: dict[str, object]) -> dict[str, object]:
    """Allow only compact operational metadata in the user-visible event stream."""
    allowed = {"stage", "status", "reason", "progress", "run_id", "artifact_id"}
    return {key: value for key, value in payload.items() if key in allowed}


class PostgresRunEvents:
    def __init__(self, session: Session, tenant: UUID, project: UUID):
        self.session, self.tenant, self.project = session, tenant, project

    def append(self, run_id: UUID, event_type: str, payload: dict[str, object]) -> RunEvent:
        if not 1 <= len(event_type) <= 96:
            raise ValueError("bounded event type is required")
        locked = self.session.execute(
            text("""SELECT id FROM research.compute_runs WHERE tenant_id=:tenant AND project_id=:project
              AND id=:run FOR UPDATE"""),
            {"tenant": self.tenant, "project": self.project, "run": run_id},
        ).scalar_one_or_none()
        if locked is None:
            raise LookupError("run not found")
        sequence = self.session.execute(
            text(
                "SELECT COALESCE(max(sequence),0)+1 FROM research.run_events WHERE tenant_id=:tenant AND run_id=:run"
            ),
            {"tenant": self.tenant, "run": run_id},
        ).scalar_one()
        safe = redact_payload(payload)
        identifier = uuid4()
        self.session.execute(
            text("""INSERT INTO research.run_events(id,tenant_id,project_id,run_id,sequence,event_type,payload)
              VALUES(:id,:tenant,:project,:run,:sequence,:type,CAST(:payload AS jsonb))"""),
            {
                "id": identifier,
                "tenant": self.tenant,
                "project": self.project,
                "run": run_id,
                "sequence": sequence,
                "type": event_type,
                "payload": json.dumps(safe, sort_keys=True),
            },
        )
        return RunEvent(identifier, sequence, event_type, safe)

    def replay(self, run_id: UUID, after: int = 0, limit: int = 100) -> list[RunEvent]:
        if after < 0 or not 1 <= limit <= 100:
            raise ValueError("invalid event cursor")
        bounds = (
            self.session.execute(
                text("""SELECT min(sequence) AS first,max(sequence) AS latest
              FROM research.run_events WHERE tenant_id=:tenant AND project_id=:project
              AND run_id=:run"""),
                {"tenant": self.tenant, "project": self.project, "run": run_id},
            )
            .mappings()
            .one()
        )
        if after > 0 and (
            bounds["latest"] is None
            or after > bounds["latest"]
            or (bounds["first"] is not None and after < bounds["first"] - 1)
        ):
            raise ValueError("event cursor is unavailable; resync from run state")
        rows = self.session.execute(
            text("""SELECT id,sequence,event_type,payload FROM research.run_events WHERE tenant_id=:tenant
              AND project_id=:project AND run_id=:run AND sequence>:after ORDER BY sequence LIMIT :limit"""),
            {
                "tenant": self.tenant,
                "project": self.project,
                "run": run_id,
                "after": after,
                "limit": limit,
            },
        ).mappings()
        return [
            RunEvent(row["id"], row["sequence"], row["event_type"], row["payload"]) for row in rows
        ]
