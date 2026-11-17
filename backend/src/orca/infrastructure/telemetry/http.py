"""Purpose: emit P08 request timing without token, path, query, body or subject leakage.
Behavior: log a server route template and server-generated request ID only.
Refs: main_implementation plan.md P08.1; api design.md §§10,14; Phase: P08.
"""

import logging
from time import perf_counter

from fastapi import Request

LOGGER = logging.getLogger("orca.telemetry")


def emit_http(request: Request, request_id: str, status: int, started: float) -> None:
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    # Only templates registered by the application are accepted. Actual request
    # paths and unmatched URLs may carry bearer tokens or private identifiers.
    registered = {getattr(candidate, "path", None) for candidate in request.app.routes}
    safe_route = template if template is not None and template in registered else "unmatched"
    method = (
        request.method
        if request.method in {"GET", "POST", "PATCH", "DELETE", "OPTIONS"}
        else "OTHER"
    )
    duration_ms = max(0, min(round((perf_counter() - started) * 1000), 3_600_000))
    LOGGER.info(
        "http_request method=%s route=%s status=%d duration_ms=%d request_id=%s",
        method,
        safe_route,
        status,
        duration_ms,
        request_id,
    )
