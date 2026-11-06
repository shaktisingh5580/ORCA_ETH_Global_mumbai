"""Purpose: expose implemented P03 product reads under validated organization membership.
Behavior: intersect token and role scopes, use RLS and conceal inaccessible products.
Refs: API 3.4; P03-research-scope.json; P03-catalog-read-contract.json; Phase: P03.5.
"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import TypeAdapter, ValidationError

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.projects import engine, resolve_tenant
from orca.application.catalog import CatalogService
from orca.contracts.catalog import (
    CanonicalVariableMapping,
    LicenseProfile,
    ProductCoverage,
    ProductVersion,
    ScientificProduct,
    SourceProvider,
)
from orca.contracts.common import Latitude, Longitude, Page, UTCDateTime
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork

router = APIRouter(prefix="/v1/catalog", tags=["catalog"])


@router.get("/providers", response_model=Page[SourceProvider])
def list_providers(
    request: Request,
    principal: CurrentPrincipal,
    organization_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
) -> Page[SourceProvider]:
    tenant = authorize(request, principal, organization_id)
    if set(request.query_params) - {"organization_id", "limit", "cursor", "q"}:
        raise HTTPException(422, "Unsupported provider filter")
    with TenantUnitOfWork(engine(request), tenant) as session:
        rows = CatalogService(session, tenant).providers(after=cursor, query=q, limit=limit + 1)
        return Page[SourceProvider](
            items=rows[:limit],
            page_size=limit,
            next_cursor=rows[limit - 1].id if len(rows) > limit else None,
        )


def selected_version(
    request: Request,
    principal: CurrentPrincipal,
    organization_id: UUID,
    product_id: UUID,
    version: UUID | None,
) -> ProductVersion | None:
    tenant = authorize(request, principal, organization_id)
    if set(request.query_params) - {"organization_id", "version"}:
        raise HTTPException(422, "Unsupported evidence query parameter")
    with TenantUnitOfWork(engine(request), tenant) as session:
        service = CatalogService(session, tenant)
        if service.product(product_id) is None:
            raise HTTPException(404, "Product not found")
        rows = service.versions(product_id, version_id=version, limit=2)
        if version is not None and not rows:
            raise HTTPException(404, "Product version not found")
        if len(rows) > 1:
            raise HTTPException(409, "Select an explicit product version")
        return rows[0] if rows else None


@router.get("/products/{product_id}/coverage", response_model=ProductCoverage)
def product_coverage(
    product_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    organization_id: UUID,
    version: UUID | None = None,
) -> ProductCoverage:
    selected = selected_version(request, principal, organization_id, product_id, version)
    return ProductCoverage(
        product_id=product_id,
        version_id=selected.id if selected else None,
        coverage=selected.coverage if selected else None,
    )


@router.get("/products/{product_id}/licenses", response_model=LicenseProfile)
def product_license(
    product_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    organization_id: UUID,
    version: UUID | None = None,
) -> LicenseProfile:
    selected = selected_version(request, principal, organization_id, product_id, version)
    return selected.license if selected else LicenseProfile()


def authorize(request: Request, principal: CurrentPrincipal, organization_id: UUID) -> UUID:
    tenant, roles = resolve_tenant(engine(request), principal, organization_id)
    try:
        principal.require("dataset:read", roles)
    except PermissionError as exc:
        raise HTTPException(403, "Dataset read scope required") from exc
    return tenant


@router.get("/products", response_model=Page[ScientificProduct])
def list_products(
    request: Request,
    principal: CurrentPrincipal,
    organization_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
    provider_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    kind: Literal["observation", "reanalysis", "forecast", "derived", "static"] | None = None,
    variable: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    bbox: Annotated[str | None, Query(max_length=160)] = None,
    start: UTCDateTime | None = None,
    end: UTCDateTime | None = None,
) -> Page[ScientificProduct]:
    tenant = authorize(request, principal, organization_id)
    supported = {
        "organization_id",
        "limit",
        "cursor",
        "provider_id",
        "kind",
        "variable",
        "bbox",
        "start",
        "end",
    }
    if set(request.query_params) - supported:
        raise HTTPException(422, "Unsupported catalog filter in this implementation slice")
    bounds = None
    if bbox is not None:
        try:
            bounds = TypeAdapter(tuple[Longitude, Latitude, Longitude, Latitude]).validate_python(
                tuple(float(part) for part in bbox.split(","))
            )
            if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
                raise ValueError("unordered box")
        except (ValueError, ValidationError) as exc:
            raise HTTPException(
                422, "Expected west,south,east,north without antimeridian crossing"
            ) from exc
    if start is not None and end is not None and end <= start:
        raise HTTPException(422, "end must be after start")
    with TenantUnitOfWork(engine(request), tenant) as session:
        rows = CatalogService(session, tenant).products(
            after=cursor,
            limit=limit + 1,
            provider_id=provider_id,
            kind=kind,
            variable=variable,
            bbox=bounds,
            start=start,
            end=end,
        )
        return Page[ScientificProduct](
            items=rows[:limit],
            page_size=limit,
            next_cursor=str(rows[limit - 1].id) if len(rows) > limit else None,
        )


@router.get("/products/{product_id}", response_model=ScientificProduct)
def get_product(
    product_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    organization_id: UUID,
) -> ScientificProduct:
    tenant = authorize(request, principal, organization_id)
    with TenantUnitOfWork(engine(request), tenant) as session:
        product = CatalogService(session, tenant).product(product_id)
        if product is None:
            raise HTTPException(404, "Product not found")
        return product


@router.get("/products/{product_id}/versions", response_model=Page[ProductVersion])
def list_versions(
    product_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    organization_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
) -> Page[ProductVersion]:
    tenant = authorize(request, principal, organization_id)
    if set(request.query_params) - {"organization_id", "limit", "cursor"}:
        raise HTTPException(422, "Unsupported version-list filter")
    with TenantUnitOfWork(engine(request), tenant) as session:
        service = CatalogService(session, tenant)
        if service.product(product_id) is None:
            raise HTTPException(404, "Product not found")
        rows = service.versions(product_id, after=cursor, limit=limit + 1)
        return Page[ProductVersion](
            items=rows[:limit],
            page_size=limit,
            next_cursor=str(rows[limit - 1].id) if len(rows) > limit else None,
        )


@router.get("/products/{product_id}/variables", response_model=Page[CanonicalVariableMapping])
def list_variables(
    product_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    organization_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
) -> Page[CanonicalVariableMapping]:
    versions = list_versions(product_id, request, principal, organization_id, limit, cursor)
    return Page[CanonicalVariableMapping](
        items=[CanonicalVariableMapping.from_version(version) for version in versions.items],
        next_cursor=versions.next_cursor,
        page_size=versions.page_size,
    )
