"""Purpose: manage database-backed companion work and lease fences.
Behavior: deduplicate submissions and avoid automatic repeat calls after ambiguous interruptions.
Refs: main implementation plan P12.2; A-API §§3.13,17.
"""

import hashlib
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import text

from orca.application.companion import Conversations
from orca.application.companion_reply import Reply, ReplyRequest
from orca.contracts.common import Contract

State = Literal["queued", "running", "completed", "expired", "interrupted", "rate_limited"]


class Job(Contract):
    id: UUID
    state: State
    result: Reply | None = None


class JobConflict(Exception):
    pass


@dataclass(frozen=True)
class Claim:
    lease: UUID
    body: ReplyRequest


class Jobs:
    def __init__(self, repo: Conversations, conversation: UUID, identifier: UUID) -> None:
        self.session = repo.session
        self.params = {"conversation": conversation, "id": identifier}
        self.identifier = identifier

    def expire(self) -> None:
        self.session.execute(
            text("""UPDATE research.companion_reply_jobs
          SET state=CASE WHEN state='running' THEN 'interrupted' ELSE 'expired' END,
              input=NULL,lease=NULL,lease_until=NULL
          WHERE conversation_id=:conversation AND id=:id AND
          ((state='queued' AND expires_at<=clock_timestamp()) OR
           (state='running' AND lease_until<=clock_timestamp()))"""),
            self.params,
        )

    def get(self) -> Job:
        self.expire()
        row = (
            self.session.execute(
                text("""SELECT id,state,result FROM research.companion_reply_jobs
          WHERE conversation_id=:conversation AND id=:id"""),
                self.params,
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("Job not found")
        return Job.model_validate(row)

    def enqueue(self, body: ReplyRequest) -> Job:
        payload = body.model_dump_json()
        digest = hashlib.sha256(payload.encode()).hexdigest()
        self.session.execute(
            text("""INSERT INTO research.companion_reply_jobs
          (conversation_id,id,input_digest,input)
          VALUES (:conversation,:id,:digest,CAST(:payload AS jsonb))
          ON CONFLICT (conversation_id,id) DO NOTHING"""),
            {**self.params, "digest": digest, "payload": payload},
        )
        stored: object = self.session.execute(
            text("""SELECT input_digest FROM research.companion_reply_jobs
          WHERE conversation_id=:conversation AND id=:id"""),
            self.params,
        ).scalar_one()
        if stored != digest:
            raise JobConflict
        return self.get()

    def claim(self) -> Claim | None:
        self.expire()
        lease = uuid4()
        row = self.session.execute(
            text("""UPDATE research.companion_reply_jobs
          SET state='running',lease=:lease,lease_until=clock_timestamp()+interval '90 seconds'
          WHERE conversation_id=:conversation AND id=:id AND state='queued'
            AND expires_at>clock_timestamp() RETURNING input"""),
            {**self.params, "lease": lease},
        ).scalar_one_or_none()
        return Claim(lease, ReplyRequest.model_validate(row)) if row is not None else None

    def finish(self, lease: UUID, result: Reply | None) -> bool:
        row = self.session.execute(
            text("""UPDATE research.companion_reply_jobs
          SET state=:state,input=NULL,lease=NULL,lease_until=NULL,result=CAST(:result AS jsonb)
          WHERE conversation_id=:conversation AND id=:id AND state='running'
            AND lease=:lease AND lease_until>clock_timestamp() RETURNING id"""),
            {
                **self.params,
                "lease": lease,
                "state": "completed" if result is not None else "rate_limited",
                "result": result.model_dump_json() if result is not None else None,
            },
        ).scalar_one_or_none()
        return row is not None
