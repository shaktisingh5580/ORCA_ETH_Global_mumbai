"""Purpose: serve only the four implemented P01 metadata/health operations.
Behavior: readiness fails closed; no science, auth-for-users or provider stub routes.
Refs: API section 3.1; docs/decisions/P01-contract-reconciliation.md; Phase: P01.
"""

import secrets
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from orca.api.errors import Problem, problem_response, validation_error
from orca.api.routers.projects import router as projects_router
from orca.config import Settings
from orca.contracts.common import APIMetadata, Liveness, ReadinessReport
from orca.infrastructure.auth.oidc import OIDCVerifier
from orca.infrastructure.db.engine import create_runtime_engine


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(
        title="ORCA P01", version="0.1.0", openapi_url=None, docs_url=None, redoc_url=None
    )
    app.add_exception_handler(RequestValidationError, validation_error)  # type: ignore[arg-type]
    if settings.database_url:
        app.state.runtime_engine = create_runtime_engine(settings.database_url)
    if settings.oidc_issuer and settings.oidc_audience and settings.oidc_jwks_url:
        app.state.oidc_verifier = OIDCVerifier(
            settings.oidc_issuer, settings.oidc_audience, settings.oidc_jwks_url
        )
    app.include_router(projects_router)

    @app.middleware("http")
    async def request_id(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    def permitted(request: Request) -> bool:
        actual = request.headers.get("Authorization", "")
        expected = "Bearer " + settings.monitoring_token.get_secret_value()
        return secrets.compare_digest(actual.encode(), expected.encode())

    def denied(request: Request) -> JSONResponse:
        response = problem_response(
            Problem(
                type="urn:orca:problem:unauthorized",
                title="Unauthorized",
                status=401,
                detail="Monitoring authorization required",
                code="UNAUTHENTICATED",
                request_id=request.state.request_id,
            )
        )
        response.headers["WWW-Authenticate"] = "Bearer"
        return response

    metadata: dict[str, Any] = {
        "x-orca-phase": "P01",
        "x-orca-implementation-status": "IMPLEMENTED",
    }
    monitor = HTTPBearer(
        scheme_name="MonitoringBearer",
        auto_error=False,
        description="Operational monitoring token; not application user authentication",
    )
    unauthorized = {
        "description": "Monitoring authorization required",
        "content": {"application/problem+json": {"schema": Problem.model_json_schema()}},
    }

    @app.get("/livez", response_model=Liveness, operation_id="get_livez", openapi_extra=metadata)
    def live() -> Liveness:
        return Liveness()

    @app.get(
        "/readyz",
        response_model=ReadinessReport,
        operation_id="get_readyz",
        responses={401: unauthorized, 503: {"model": ReadinessReport}},
        dependencies=[Depends(monitor)],
        openapi_extra=metadata,
    )
    def ready(request: Request) -> Response:
        if not permitted(request):
            return denied(request)
        body = ReadinessReport(
            components={"core_backend": "not_implemented"},
            release_profile="core-C",
            blocked_gates=[f"G{i:02}" for i in range(2, 9)],
        )
        return JSONResponse(body.model_dump(mode="json"), status_code=503)

    @app.get(
        "/v1/meta", response_model=APIMetadata, operation_id="get_meta", openapi_extra=metadata
    )
    def meta() -> APIMetadata:
        return APIMetadata(
            revision="3.1.0-proposal", release_profile="P01-foundation", enabled_capabilities=[]
        )

    @app.get(
        "/openapi.json",
        operation_id="get_openapi",
        openapi_extra=metadata,
        responses={401: unauthorized},
        dependencies=[] if settings.docs_public else [Depends(monitor)],
    )
    def schema(request: Request) -> Response:
        if not settings.docs_public and not permitted(request):
            return denied(request)
        return JSONResponse(app.openapi())

    return app
