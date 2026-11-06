"""Purpose: serve tenant-authorized metadata fitness and baseline compatibility checks.
Behavior: read pinned stored versions only; return unknown instead of fabricated compatibility.
Refs: API 3.4; main plan P03.5; Phase: P03.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.projects import engine, resolve_tenant
from orca.application.fitness import check, compare_versions, fitness
from orca.contracts.catalog import ProductVersion
from orca.contracts.fitness import (
    CompatibilityReport,
    DataCompatibilityRequest,
    FitnessCheck,
    FitnessReport,
    FitnessRequest,
)
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork

router = APIRouter(prefix="/v1/catalog", tags=["scientific metadata checks"])


def planning_tenant(request: Request, principal: CurrentPrincipal, organization: UUID) -> UUID:
    tenant, roles = resolve_tenant(engine(request), principal, organization)
    try:
        principal.require("research:plan", roles)
    except PermissionError as exc:
        raise HTTPException(403, "Research planning scope required") from exc
    return tenant


def version(session: Session, tenant: UUID, identifier: UUID) -> ProductVersion:
    stored: Any = session.execute(
        text("SELECT manifest FROM research.catalog_versions WHERE tenant_id=:tenant AND id=:id"),
        {"tenant": tenant, "id": identifier},
    ).scalar_one_or_none()
    if stored is None:
        raise HTTPException(404, "Product version not found")
    result = ProductVersion.model_validate(stored)
    if result.id != identifier or result.tenant_id != tenant:
        raise ValueError("stored version identity mismatch")
    return result


@router.post("/products/{product_id}/fitness-checks", response_model=FitnessReport)
def fitness_check(
    product_id: UUID, body: FitnessRequest, request: Request, principal: CurrentPrincipal
) -> FitnessReport:
    tenant = planning_tenant(request, principal, body.organization_id)
    with TenantUnitOfWork(engine(request), tenant) as session:
        selected = version(session, tenant, body.version_ref)
        if selected.product_id != product_id:
            raise HTTPException(404, "Product version not found")
        return fitness(selected, body)


@router.post("/compatibility-checks", response_model=CompatibilityReport)
def compatibility_check(
    body: DataCompatibilityRequest, request: Request, principal: CurrentPrincipal
) -> CompatibilityReport:
    tenant = planning_tenant(request, principal, body.organization_id)
    with TenantUnitOfWork(engine(request), tenant) as session:
        baseline = version(session, tenant, body.baseline_ref)
        checks: list[FitnessCheck] = []
        for identifier in dict.fromkeys(body.products):
            product = version(session, tenant, identifier)
            report = fitness(
                product,
                FitnessRequest(
                    organization_id=body.organization_id,
                    method_id=body.method_id,
                    version_ref=identifier,
                    geo_scope=body.geo_scope,
                    time_window=body.time_window,
                ),
            )
            checks.extend(
                c.model_copy(update={"name": f"{identifier}:{c.name}"})
                for c in report.checks + compare_versions(product, baseline)
            )
        if not baseline.license.can_preserve:
            checks.append(check("baseline_rights", False, "Baseline preservation is not permitted"))
        return CompatibilityReport(
            status="incompatible" if any(c.status == "fail" for c in checks) else "unknown",
            method_id=body.method_id,
            version_refs=body.products,
            baseline_ref=baseline.id,
            checks=checks,
        )
