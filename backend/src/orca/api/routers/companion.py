"""Purpose: serve the opt-in private companion conversation API.
Behavior: require verified identity and tenant membership, and guard writes with ETags.
Refs: main implementation plan P12.1; A-API §3.13.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from sqlalchemy import text

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.projects import engine, require_scope, resolve_tenant
from orca.application.companion import (
    Conversation,
    ConversationChanged,
    ConversationPage,
    Conversations,
    CreateConversation,
    PatchConversation,
)
from orca.domain.identity import Principal
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork

router = APIRouter(prefix="/v1/conversations", tags=["companion"])


@contextmanager
def service(
    request: Request, principal: Principal, organization_id: UUID
) -> Iterator[Conversations]:
    db = engine(request)
    tenant, roles = resolve_tenant(db, principal, organization_id)
    require_scope(principal, "org:read", roles)
    with TenantUnitOfWork(db, tenant) as session:
        role = session.execute(
            text("""SELECT role FROM iam.memberships
          WHERE tenant_id=:tenant AND subject=:subject AND status='active' FOR SHARE"""),
            {"tenant": tenant, "subject": principal.subject},
        ).scalar_one_or_none()
        if role is None:
            raise HTTPException(404, "Organization not found")
        require_scope(principal, "org:read", {role})
        try:
            yield Conversations(session, tenant, principal)
        except LookupError as exc:
            raise HTTPException(404, "Conversation not found") from exc
        except ConversationChanged as exc:
            raise HTTPException(412, "Conversation version is stale") from exc


def set_etag(response: Response, result: Conversation) -> None:
    response.headers["ETag"] = f'"{result.version}"'


@router.get("")
def list_conversations(
    request: Request,
    principal: CurrentPrincipal,
    organization_id: UUID,
    cursor: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> ConversationPage:
    with service(request, principal, organization_id) as conversations:
        result = conversations.list(cursor, limit)
    return result


@router.post("", status_code=201)
def create_conversation(
    body: CreateConversation,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    organization_id: UUID,
) -> Conversation:
    with service(request, principal, organization_id) as conversations:
        result = conversations.create(body)
    set_etag(response, result)
    return result


@router.get("/{conversation_id}")
def get_conversation(
    conversation_id: UUID,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    organization_id: UUID,
) -> Conversation:
    with service(request, principal, organization_id) as conversations:
        result = conversations.get(conversation_id)
    set_etag(response, result)
    return result


@router.patch("/{conversation_id}")
def update_conversation(
    conversation_id: UUID,
    body: PatchConversation,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    organization_id: UUID,
    if_match: Annotated[str | None, Header()] = None,
) -> Conversation:
    with service(request, principal, organization_id) as conversations:
        if if_match is None:
            raise HTTPException(428, "If-Match is required")
        result = conversations.update(conversation_id, body, if_match)
    set_etag(response, result)
    return result
