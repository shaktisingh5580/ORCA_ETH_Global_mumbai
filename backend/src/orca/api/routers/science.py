"""Purpose: expose P04 scientific registry and metadata-only recipe checks.
Behavior: enforce organization scopes; hide unapproved methods and never label metadata validation a finding.
Refs: main_implementation plan.md P04.1–P04.4; api design.md §3.5; Phase: P04.
"""

import base64
import binascii
from collections.abc import Callable
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.projects import engine, resolve_tenant
from orca.application.fitness import compare_versions
from orca.contracts.catalog import ProductVersion
from orca.contracts.science_api import (
    QualityCodebook,
    RecipeTemplatePage,
    RecipeTemplateView,
    RecipeValidationCheck,
    RecipeValidationReport,
    RecipeValidationRequest,
    ScientificMethodDetail,
    ScientificMethodPage,
    ScientificMethodView,
    ScientificPolicySummary,
)
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.science.quality import validate_sst_recipe
from orca.science.registry import METHOD_REGISTRY, MethodRegistry, MethodVersion

router = APIRouter(prefix="/v1/science", tags=["scientific methods"])


def _query(request: Request, allowed: set[str]) -> None:
    if set(request.query_params) - allowed:
        raise HTTPException(422, "Unsupported science query parameter")


def _tenant(
    request: Request, principal: CurrentPrincipal, organization_id: UUID, scope: str
) -> UUID:
    tenant, roles = resolve_tenant(engine(request), principal, organization_id)
    try:
        principal.require(scope, roles)
    except PermissionError as error:
        raise HTTPException(403, f"{scope} scope required") from error
    return tenant


def _registry(request: Request) -> MethodRegistry:
    return cast(MethodRegistry, getattr(request.app.state, "science_registry", METHOD_REGISTRY))


def _method_view(method: MethodVersion) -> ScientificMethodView:
    return ScientificMethodView(
        method_id=method.method_id,
        version=method.version,
        code_digest=method.code_digest,
        required_baseline_years=method.required_baseline_years,
        measurement_semantics=method.measurement_semantics,
        qc_policy_id=method.qc_policy_id,
        qc_policy_digest=method.qc_policy_digest,
        calendar=method.calendar,
    )


def _cursor(identifier: str) -> str:
    return base64.urlsafe_b64encode(identifier.encode("utf-8")).decode("ascii").rstrip("=")


def _after(cursor: str | None) -> str | None:
    if cursor is None:
        return None
    try:
        encoded = cursor + "=" * (-len(cursor) % 4)
        identifier = base64.urlsafe_b64decode(encoded).decode("utf-8")
        if not identifier or _cursor(identifier) != cursor:
            raise ValueError("non-canonical cursor")
        return identifier
    except (binascii.Error, ValueError, UnicodeDecodeError) as error:
        raise HTTPException(422, "Invalid science page cursor") from error


def _page[T](
    items: tuple[T, ...], *, cursor: str | None, limit: int, key: Callable[[T], str]
) -> tuple[tuple[T, ...], str | None]:
    after = _after(cursor)
    filtered = tuple(item for item in items if after is None or key(item) > after)
    selected = filtered[:limit]
    next_cursor = _cursor(key(selected[-1])) if len(filtered) > limit else None
    return selected, next_cursor


@router.get("/methods", response_model=ScientificMethodPage)
def methods(
    request: Request,
    principal: CurrentPrincipal,
    organization_id: UUID,
    kind: Literal["sst_anomaly"] | None = None,
    status: Literal["approved"] | None = None,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ScientificMethodPage:
    _tenant(request, principal, organization_id, "dataset:read")
    _query(request, {"organization_id", "kind", "status", "cursor", "limit"})
    _ = kind, status
    entries = tuple(_method_view(method) for method in _registry(request).approved_versions())
    selected, next_cursor = _page(
        entries, cursor=cursor, limit=limit, key=lambda item: f"{item.method_id}\x00{item.version}"
    )
    return ScientificMethodPage(items=selected, page_size=limit, next_cursor=next_cursor)


@router.get("/methods/{method_id}", response_model=ScientificMethodDetail)
def method_detail(
    method_id: str, request: Request, principal: CurrentPrincipal, organization_id: UUID
) -> ScientificMethodDetail:
    _tenant(request, principal, organization_id, "dataset:read")
    _query(request, {"organization_id"})
    versions = tuple(
        _method_view(method)
        for method in _registry(request).approved_versions()
        if method.method_id == method_id
    )
    if not versions:
        raise HTTPException(404, "Approved method not found")
    return ScientificMethodDetail(method_id=method_id, approved_versions=versions)


@router.get("/methods/{method_id}/versions/{version}", response_model=ScientificMethodView)
def method_version(
    method_id: str,
    version: str,
    request: Request,
    principal: CurrentPrincipal,
    organization_id: UUID,
) -> ScientificMethodView:
    _tenant(request, principal, organization_id, "dataset:read")
    _query(request, {"organization_id"})
    for method in _registry(request).approved_versions():
        if method.method_id == method_id and method.version == version:
            return _method_view(method)
    raise HTTPException(404, "Approved method version not found")


@router.get("/recipe-templates", response_model=RecipeTemplatePage)
def templates(
    request: Request,
    principal: CurrentPrincipal,
    organization_id: UUID,
    variable: str | None = None,
    method_id: str | None = None,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> RecipeTemplatePage:
    _tenant(request, principal, organization_id, "research:plan")
    _query(request, {"organization_id", "variable", "method_id", "cursor", "limit"})
    if variable is not None and variable not in {
        "sea_surface_temperature",
        "sea_surface_foundation_temperature",
    }:
        return RecipeTemplatePage(items=(), page_size=limit)
    entries = tuple(
        RecipeTemplateView(
            template_id=f"{method.method_id}@{method.version}",
            method_id=method.method_id,
            method_version=method.version,
            method_digest=method.code_digest,
            required_baseline_years=method.required_baseline_years,
            qc_policy_id=method.qc_policy_id,
            qc_policy_digest=method.qc_policy_digest,
            calendar=method.calendar,
            measurement_semantics=method.measurement_semantics,
        )
        for method in _registry(request).approved_versions()
        if method_id is None or method.method_id == method_id
    )
    selected, next_cursor = _page(
        entries, cursor=cursor, limit=limit, key=lambda item: item.template_id
    )
    return RecipeTemplatePage(items=selected, page_size=limit, next_cursor=next_cursor)


@router.get("/recipe-templates/{template_id}", response_model=RecipeTemplateView)
def template_detail(
    template_id: str, request: Request, principal: CurrentPrincipal, organization_id: UUID
) -> RecipeTemplateView:
    _tenant(request, principal, organization_id, "research:plan")
    _query(request, {"organization_id"})
    for method in _registry(request).approved_versions():
        if f"{method.method_id}@{method.version}" == template_id:
            return RecipeTemplateView(
                template_id=template_id,
                method_id=method.method_id,
                method_version=method.version,
                method_digest=method.code_digest,
                required_baseline_years=method.required_baseline_years,
                qc_policy_id=method.qc_policy_id,
                qc_policy_digest=method.qc_policy_digest,
                calendar=method.calendar,
                measurement_semantics=method.measurement_semantics,
            )
    raise HTTPException(404, "Approved recipe template not found")


@router.get("/policies", response_model=ScientificPolicySummary)
def policy(
    request: Request,
    principal: CurrentPrincipal,
    organization_id: UUID,
    method_id: str | None = None,
    version: str | None = None,
) -> ScientificPolicySummary:
    _tenant(request, principal, organization_id, "research:plan")
    _query(request, {"organization_id", "method_id", "version"})
    if method_id is None or version is None:
        raise HTTPException(422, "Select an exact method ID and version")
    selected_method = next(
        (
            method
            for method in _registry(request).approved_versions()
            if method.method_id == method_id and method.version == version
        ),
        None,
    )
    if selected_method is None:
        raise HTTPException(404, "Approved method policy not found")
    policies = cast(
        dict[tuple[str, str], ScientificPolicySummary],
        getattr(request.app.state, "science_policies", {}),
    )
    result = policies.get((method_id, version))
    if (
        result is None
        or result.threshold_status != "approved"
        or result.method_id != method_id
        or result.version != version
        or result.qc_policy_id != selected_method.qc_policy_id
        or result.qc_policy_digest != selected_method.qc_policy_digest
    ):
        raise HTTPException(404, "Reviewed scientific thresholds not found")
    return result


@router.get("/quality-codebook", response_model=QualityCodebook)
def quality_codebook(
    request: Request,
    principal: CurrentPrincipal,
    organization_id: UUID,
    source_id: str | None = None,
    variable: str | None = None,
) -> QualityCodebook:
    _tenant(request, principal, organization_id, "dataset:read")
    _query(request, {"organization_id", "source_id", "variable"})
    if source_id is None or variable is None:
        raise HTTPException(422, "Select an exact source and variable")
    codebooks = cast(
        dict[tuple[str, str], QualityCodebook],
        getattr(request.app.state, "science_codebooks", {}),
    )
    result = codebooks.get((source_id, variable))
    if (
        result is None
        or result.review_status != "approved"
        or result.source_id != source_id
        or result.variable != variable
    ):
        raise HTTPException(404, "Reviewed quality codebook not found")
    return result


def _source_checks(
    current: ProductVersion, baseline: ProductVersion, body: RecipeValidationRequest
) -> tuple[RecipeValidationCheck, ...]:
    recipe = body.recipe
    checks: list[RecipeValidationCheck] = []
    for item in compare_versions(current, baseline):
        if item.name == "units":
            continue  # The reviewed P04 recipe allows explicit Kelvin/Celsius conversion.
        checks.append(RecipeValidationCheck(name=item.name, status=item.status, reason=item.reason))
    for label, source, unit, measurement, depth in (
        (
            "current",
            current,
            recipe.current_unit,
            recipe.current_measurement,
            recipe.current_depth_m,
        ),
        (
            "baseline",
            baseline,
            recipe.baseline_unit,
            recipe.baseline_measurement,
            recipe.baseline_depth_m,
        ),
    ):
        matches = (
            source.calendar == recipe.calendar
            and source.native_units == unit
            and source.physical_definition == measurement
            and source.depth_m is not None
            and source.depth_m == depth
            and source.license.can_preserve
            and source.available_at is not None
            and recipe.as_of is not None
            and source.available_at <= recipe.as_of
        )
        checks.append(
            RecipeValidationCheck(
                name=f"{label}_metadata",
                status="pass" if matches else "fail",
                reason="Pinned calendar, units, measurement, depth, rights and availability must match",
            )
        )
    return tuple(checks)


@router.post("/recipe-validations", response_model=RecipeValidationReport)
def validate_recipe(
    body: RecipeValidationRequest, request: Request, principal: CurrentPrincipal
) -> RecipeValidationReport:
    tenant = _tenant(request, principal, body.organization_id, "research:plan")
    _query(request, set())
    try:
        validate_sst_recipe(body.recipe, _registry(request))
    except ValueError as error:
        return RecipeValidationReport(
            status="invalid",
            method_id=body.recipe.method_id,
            method_version=body.recipe.method_version,
            selected_sources=body.selected_sources,
            checks=(
                RecipeValidationCheck(
                    name="approved_method_and_recipe", status="fail", reason=str(error)
                ),
            ),
        )
    policies = cast(
        dict[tuple[str, str], ScientificPolicySummary],
        getattr(request.app.state, "science_policies", {}),
    )
    reviewed = policies.get((body.recipe.method_id, body.recipe.method_version))
    if (
        reviewed is None
        or reviewed.threshold_status != "approved"
        or reviewed.qc_policy_id != body.recipe.current_qc_policy_id
        or reviewed.qc_policy_digest != body.recipe.current_qc_policy_digest
    ):
        return RecipeValidationReport(
            status="invalid",
            method_id=body.recipe.method_id,
            method_version=body.recipe.method_version,
            selected_sources=body.selected_sources,
            checks=(
                RecipeValidationCheck(
                    name="reviewed_policy",
                    status="fail",
                    reason="Exact method thresholds and QC policy are not approved",
                ),
            ),
        )
    from orca.api.routers.fitness import version as selected_version

    with TenantUnitOfWork(engine(request), tenant) as session:
        current = selected_version(session, tenant, body.selected_sources.current_version_id)
        baseline = selected_version(session, tenant, body.selected_sources.baseline_version_id)
    checks = _source_checks(current, baseline, body)
    return RecipeValidationReport(
        status="incompatible"
        if any(check.status == "fail" for check in checks)
        else "requires_source_validation",
        method_id=body.recipe.method_id,
        method_version=body.recipe.method_version,
        selected_sources=body.selected_sources,
        checks=checks,
    )
