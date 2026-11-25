"""Purpose: persist and paginate immutable cited replies under conversation RLS.
Behavior: record only verified answer text and its artifact identity; the API rechecks access.
Refs: main implementation plan P12.1–P12.2; A-API §3.13.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.application.companion_reply import Reply
from orca.contracts.common import Contract


class SavedReply(Contract):
    id: UUID
    artifact_id: UUID
    manifest_digest: str
    body: str
    created_at: datetime


class ReplyHistory(Contract):
    items: list[Reply]
    next_cursor: UUID | None


def save(session: Session, conversation: UUID, result: Reply) -> UUID:
    citation = result.citation
    if result.status != "answered" or citation is None:
        raise ValueError("Only cited answers can be saved")
    identifier = uuid4()
    session.execute(
        text("""INSERT INTO research.companion_replies
        (id,conversation_id,artifact_id,manifest_digest,body)
        VALUES (:id,:conversation,:artifact,:digest,:body)"""),
        {
            "id": identifier,
            "conversation": conversation,
            "artifact": citation.artifact_id,
            "digest": citation.manifest_digest,
            "body": result.text,
        },
    )
    return identifier


def page(session: Session, conversation: UUID, cursor: UUID | None, limit: int) -> list[SavedReply]:
    rows = session.execute(
        text("""SELECT id,artifact_id,manifest_digest,body,created_at
        FROM research.companion_replies WHERE conversation_id=:conversation
        AND (CAST(:cursor AS uuid) IS NULL OR id>CAST(:cursor AS uuid))
        ORDER BY id LIMIT :limit"""),
        {"conversation": conversation, "cursor": cursor, "limit": limit + 1},
    ).mappings()
    return [SavedReply.model_validate(row) for row in rows]
