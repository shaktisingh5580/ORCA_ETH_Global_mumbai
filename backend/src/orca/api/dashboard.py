"""Purpose: serve the research workspace beside the existing Oracle API.
Behavior: expose only public login configuration and static assets; reuse authenticated project APIs.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A05; Phase: A05 dashboard increment.
"""

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from orca.config import Settings


def install_dashboard(app: FastAPI, settings: Settings) -> None:
    folder = Path(__file__).resolve().parent / "dashboard"
    app.mount("/research/assets", StaticFiles(directory=folder), name="research-assets")

    @app.get("/research", include_in_schema=False)
    @app.get("/research/", include_in_schema=False)
    def workspace() -> FileResponse:
        return FileResponse(
            folder / "index.html",
            headers={
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self' https://*.supabase.co; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-cache",
            },
        )

    @app.get("/research/config", include_in_schema=False)
    def configuration() -> dict[str, Any]:
        return {
            "auth_url": settings.oidc_issuer if settings.auth_mode == "supabase" else None,
            "anon_key": settings.supabase_anon_key.get_secret_value()
            if settings.auth_mode == "supabase" and settings.supabase_anon_key
            else None,
            "reasoning_configured": bool((settings.gemini_api_keys or settings.gemini_api_key)),
            "arkiv_configured": settings.network_enabled,
            "model": settings.gemini_model
            if (settings.gemini_api_keys or settings.gemini_api_key)
            else None,
            "profile": "bounded research prototype",
            "agent_onboarding_enabled": settings.agent_onboarding_enabled,
        }
