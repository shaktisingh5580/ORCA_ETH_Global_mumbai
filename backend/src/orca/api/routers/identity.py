"""Purpose: supply the signed-in principal and authorized workspace directory to the frontend.
Behavior: derive identity from verified tokens and list only active server-side memberships.
Refs: API 3.2 GET me/organizations; Phase: P02 frontend integration follow-up.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.projects import engine
from orca.contracts.common import Contract, Page

router = APIRouter(prefix="/v1", tags=["current user and workspaces"])


class OrganizationSummary(Contract):
    id: UUID
    tenant_id: UUID
    name: str
    slug: str
    visibility: str
    role: str


class CurrentUser(Contract):
    user_id: str
    orgs: list[OrganizationSummary]
    roles: list[str]
    capabilities: list[str]
    next_cursor: str | None = None


def memberships(
    request: Request, principal: CurrentPrincipal, after: UUID | None, limit: int
) -> list[OrganizationSummary]:
    with engine(request).connect() as connection:
        rows = connection.execute(
            text("SELECT * FROM iam.principal_organizations(:subject,:after,:limit)"),
            {"subject": principal.subject, "after": after, "limit": limit},
        ).mappings()
        return [OrganizationSummary.model_validate(dict(row)) for row in rows]


@router.get("/me", response_model=CurrentUser)
def me(request: Request, principal: CurrentPrincipal) -> CurrentUser:
    rows = memberships(request, principal, None, 101)
    selected = rows[:100]
    roles = {row.role for row in selected}
    return CurrentUser(
        user_id=principal.subject,
        orgs=selected,
        roles=sorted(roles),
        capabilities=sorted(principal.effective_scopes(roles)),
        next_cursor=str(selected[-1].id) if len(rows) > 100 else None,
    )


@router.get("/organizations", response_model=Page[OrganizationSummary])
def organizations(
    request: Request,
    principal: CurrentPrincipal,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
) -> Page[OrganizationSummary]:
    # Every defined membership role includes org:read; still intersect token scopes.
    if "org:read" not in principal.token_scopes:
        raise HTTPException(403, "Organization read scope required")
    rows = memberships(request, principal, cursor, limit + 1)
    if any("org:read" not in principal.effective_scopes({row.role}) for row in rows):
        raise HTTPException(403, "Organization read scope required")
    return Page[OrganizationSummary](
        items=rows[:limit],
        page_size=limit,
        next_cursor=str(rows[limit - 1].id) if len(rows) > limit else None,
    )
