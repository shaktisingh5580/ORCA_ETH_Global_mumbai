"""Purpose: persist private user messages and a bounded checkpoint for later turn processing.
Behavior: serialize appends, replay identical requests, and keep stored text untrusted.
Refs: main implementation plan P12.1; A-API §3.13; W-MEM6 §§4.1,11,17.
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import StringConstraints, field_validator
from sqlalchemy import text

from orca.application.companion import Conversations
from orca.contracts.common import Contract

MESSAGE_COLUMNS = "conversation_id,sequence,content,created_at"


class AppendMessage(Contract):
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]

    @field_validator("content")
    @classmethod
    def reject_nul(cls, content: str) -> str:
        if "\x00" in content:
            raise ValueError("Message cannot contain a null character")
        return content


class UserMessage(Contract):
    conversation_id: UUID
    sequence: int
    content: str
    created_at: datetime
    provenance: Literal["untrusted_user_input"] = "untrusted_user_input"


class MessagePage(Contract):
    items: list[UserMessage]
    next_cursor: int | None


class WorkingCheckpoint(Contract):
    conversation_id: UUID
    message_sequence: int
    updated_at: datetime
    expires_at: datetime
    state: Literal["message_saved"] = "message_saved"


class MessageConflict(Exception):
    pass


def append(repo: Conversations, identifier: UUID, key: UUID, body: AppendMessage) -> UserMessage:
    session = repo.session
    archived = session.execute(
        text("SELECT archived FROM research.companion_conversations WHERE id=:id FOR UPDATE"),
        {"id": identifier},
    ).scalar_one_or_none()
    if archived is None:
        raise LookupError("Conversation not found")
    if archived:
        raise MessageConflict("Conversation is archived")
    params = {"id": identifier, "key": key, "content": body.content}
    existing = (
        session.execute(
            text(f"""SELECT {MESSAGE_COLUMNS} FROM research.companion_messages
              WHERE conversation_id=:id AND request_id=:key"""),
            params,
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        if existing["content"] != body.content:
            raise MessageConflict("Idempotency key belongs to a different message")
        return UserMessage.model_validate(existing)
    row = (
        session.execute(
            text(f"""INSERT INTO research.companion_messages(conversation_id,sequence,request_id,content)
              SELECT :id,coalesce(max(sequence),0)+1,:key,:content
              FROM research.companion_messages WHERE conversation_id=:id
              RETURNING {MESSAGE_COLUMNS}"""),
            params,
        )
        .mappings()
        .one()
    )
    message = UserMessage.model_validate(row)
    session.execute(
        text("""INSERT INTO research.companion_checkpoints
          (conversation_id,message_sequence,updated_at,expires_at)
          VALUES (:id,:sequence,CAST(:created AS timestamptz),
                  CAST(:created AS timestamptz) + interval '1 hour')
          ON CONFLICT (conversation_id) DO UPDATE SET message_sequence=EXCLUDED.message_sequence,
          updated_at=EXCLUDED.updated_at,expires_at=EXCLUDED.expires_at"""),
        {"id": identifier, "sequence": message.sequence, "created": message.created_at},
    )
    return message


def history(repo: Conversations, identifier: UUID, cursor: int, limit: int) -> MessagePage:
    repo.get(identifier)
    rows = (
        repo.session.execute(
            text(f"""SELECT {MESSAGE_COLUMNS} FROM research.companion_messages
          WHERE conversation_id=:id AND sequence>:cursor ORDER BY sequence LIMIT :limit"""),
            {"id": identifier, "cursor": cursor, "limit": limit + 1},
        )
        .mappings()
        .all()
    )
    items = [UserMessage.model_validate(row) for row in rows[:limit]]
    return MessagePage(items=items, next_cursor=items[-1].sequence if len(rows) > limit else None)


def checkpoint(repo: Conversations, identifier: UUID) -> WorkingCheckpoint:
    row = (
        repo.session.execute(
            text("""SELECT cp.conversation_id,cp.message_sequence,cp.updated_at,cp.expires_at
          FROM research.companion_checkpoints cp JOIN research.companion_conversations c
            ON c.id=cp.conversation_id
          WHERE c.id=:id AND NOT c.archived AND cp.expires_at>clock_timestamp()"""),
            {"id": identifier},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise LookupError("Checkpoint not found")
    return WorkingCheckpoint.model_validate(row)
