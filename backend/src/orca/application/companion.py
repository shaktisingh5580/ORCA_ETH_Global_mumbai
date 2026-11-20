"""Purpose: read and update private companion conversations transactionally.
Behavior: bind server-owned identity and reject stale updates to conversation metadata.
Refs: main implementation plan P12.1; A-API §3.13; W-MEM6 §6.
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import StringConstraints, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.contracts.common import Contract
from orca.domain.identity import Principal

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
COLUMNS = "id,title,locale,archived,version,created_at,updated_at"


class CreateConversation(Contract):
    title: Title = "New conversation"
    locale: Literal["en"] = "en"


class PatchConversation(Contract):
    title: Title | None = None
    archive: bool | None = None
    locale: Literal["en"] | None = None

    @model_validator(mode="after")
    def require_change(self) -> "PatchConversation":
        if self.title is None and self.archive is None and self.locale is None:
            raise ValueError("At least one conversation property is required")
        return self


class Conversation(Contract):
    id: UUID
    title: str
    locale: Literal["en"]
    archived: bool
    version: int
    created_at: datetime
    updated_at: datetime


class ConversationPage(Contract):
    items: list[Conversation]
    next_cursor: UUID | None


class ConversationChanged(Exception):
    pass


class Conversations:
    def __init__(self, session: Session, tenant: UUID, principal: Principal) -> None:
        self.session, self.tenant, self.principal = session, tenant, principal
        session.execute(
            text("""SELECT
          set_config('orca.companion_subject', :subject, true),
          set_config('orca.companion_issuer', :issuer, true)"""),
            {"subject": principal.subject, "issuer": principal.issuer},
        )

    def create(self, body: CreateConversation) -> Conversation:
        row = (
            self.session.execute(
                text(f"""INSERT INTO research.companion_conversations
          (id,tenant_id,owner_subject,owner_issuer,title,locale)
          VALUES (:id,:tenant,:subject,:issuer,:title,:locale) RETURNING {COLUMNS}"""),
                {
                    "id": uuid4(),
                    "tenant": self.tenant,
                    "subject": self.principal.subject,
                    "issuer": self.principal.issuer,
                    **body.model_dump(),
                },
            )
            .mappings()
            .one()
        )
        return Conversation.model_validate(row)

    def get(self, identifier: UUID) -> Conversation:
        row = (
            self.session.execute(
                text(f"""SELECT {COLUMNS}
          FROM research.companion_conversations WHERE id=:id"""),
                {"id": identifier},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("Conversation not found")
        return Conversation.model_validate(row)

    def list(self, cursor: UUID | None, limit: int) -> ConversationPage:
        rows = (
            self.session.execute(
                text(f"""SELECT {COLUMNS}
          FROM research.companion_conversations
          WHERE (CAST(:cursor AS uuid) IS NULL OR id > CAST(:cursor AS uuid))
          ORDER BY id LIMIT :limit"""),
                {"cursor": cursor, "limit": limit + 1},
            )
            .mappings()
            .all()
        )
        items = [Conversation.model_validate(row) for row in rows[:limit]]
        return ConversationPage(
            items=items, next_cursor=items[-1].id if len(rows) > limit else None
        )

    def update(self, identifier: UUID, body: PatchConversation, etag: str) -> Conversation:
        current = self.get(identifier)
        if etag != f'"{current.version}"':
            raise ConversationChanged
        row = (
            self.session.execute(
                text(f"""UPDATE research.companion_conversations
          SET title=coalesce(:title,title), archived=coalesce(:archive,archived),
              locale=coalesce(:locale,locale), version=version+1, updated_at=clock_timestamp()
          WHERE id=:id AND version=:version RETURNING {COLUMNS}"""),
                {**body.model_dump(), "id": identifier, "version": current.version},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ConversationChanged
        return Conversation.model_validate(row)
