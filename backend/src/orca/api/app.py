"""Purpose: serve implemented research-foundation routes and truthful operational metadata.
Behavior: readiness fails closed; excluded Marine/chat/training routes are never registered.
Refs: API section 3.1; research-only guide sections 3 and 10.3; Phase: scope migration.
"""

import secrets
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from orca.api.errors import Problem, problem_response, validation_error
from orca.api.routers.catalog import router as catalog_router
from orca.api.routers.demo import router as demo_router
from orca.api.routers.files import router as files_router
from orca.api.routers.fitness import router as fitness_router
from orca.api.routers.identity import router as identity_router
from orca.api.routers.invitations import router as invitations_router
from orca.api.routers.organizations import router as organizations_router
from orca.api.routers.projects import router as projects_router
from orca.api.routers.snapshot_requests import router as snapshot_requests_router
from orca.api.routers.snapshots import router as snapshots_router
from orca.api.routers.source_probes import router as source_probes_router
from orca.api.routers.workspace_access import router as workspace_access_router
from orca.config import Settings
from orca.contracts.common import APIMetadata, Liveness, ReadinessReport
from orca.infrastructure.auth.access_logging import install_invitation_redaction
from orca.infrastructure.auth.oidc import OIDCVerifier
from orca.infrastructure.auth.supabase import SupabaseVerifier
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.storage.composition import snapshot_store, upload_store


def create_app(settings: Settings) -> FastAPI:
    install_invitation_redaction()
    app = FastAPI(
        title="ORCA research foundation",
        version="0.2.0",
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.add_exception_handler(RequestValidationError, validation_error)  # type: ignore[arg-type]
    if settings.cors_origins or settings.cors_local_development or settings.cors_lan_development:
        development_hosts = []
        if settings.cors_local_development:
            development_hosts.append(r"localhost|127\.0\.0\.1")
        if settings.cors_lan_development:
            development_hosts.append(
                r"10\.(?:[0-9]{1,3}\.){2}[0-9]{1,3}|192\.168\.[0-9]{1,3}\.[0-9]{1,3}|172\.(?:1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}"
            )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_origin_regex=r"^http://(?:" + "|".join(development_hosts) + r")(?::[0-9]{1,5})?$"
            if development_hosts
            else None,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Idempotency-Key",
                "If-Match",
                "X-Request-ID",
                "X-Organization-ID",
            ],
            expose_headers=["ETag", "X-Request-ID"],
        )
    app.state.snapshot_worker_enabled = settings.acquisition_enabled
    app.state.source_probes_enabled = settings.source_probes_enabled
    app.state.acquisition_sources = settings.acquisition_sources
    app.state.organization_creation_enabled = settings.organization_creation_enabled
    app.state.invitation_signing_key = settings.invitation_signing_key
    if settings.file_uploads_enabled:
        app.state.upload_store = upload_store(settings)
    if settings.research_demo_enabled:
        app.state.demo_snapshot_store = snapshot_store(settings)
    if settings.database_url:
        app.state.runtime_engine = create_runtime_engine(settings.database_url)
    if settings.auth_mode == "supabase" and settings.oidc_issuer and settings.supabase_anon_key:
        app.state.oidc_verifier = SupabaseVerifier(
            settings.oidc_issuer, settings.supabase_anon_key.get_secret_value()
        )
    elif settings.oidc_issuer and settings.oidc_audience and settings.oidc_jwks_url:
        app.state.oidc_verifier = OIDCVerifier(
            settings.oidc_issuer, settings.oidc_audience, settings.oidc_jwks_url
        )
    app.include_router(projects_router)
    app.include_router(identity_router)
    app.include_router(organizations_router)
    app.include_router(workspace_access_router)
    app.include_router(files_router)
    app.include_router(invitations_router)
    app.include_router(catalog_router)
    app.include_router(fitness_router)
    app.include_router(snapshots_router)
    app.include_router(snapshot_requests_router)
    app.include_router(source_probes_router)
    if settings.research_demo_enabled:
        app.include_router(demo_router)

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
            release_profile="research-foundation",
            blocked_gates=["G02"],
        )
        return JSONResponse(body.model_dump(mode="json"), status_code=503)

    @app.get(
        "/v1/meta", response_model=APIMetadata, operation_id="get_meta", openapi_extra=metadata
    )
    def meta() -> APIMetadata:
        return APIMetadata(
            revision="3.2.0-research-scope",
            release_profile="research-foundation",
            enabled_capabilities=[],
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
