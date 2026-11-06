"""Purpose: expose stable sanitized RFC9457 failures.
Behavior: map invalid requests without echoing input values or exception internals.
Refs: API section 2; Phase: P01.
"""

from typing import Literal

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field

from orca.contracts.common import Contract


class Problem(Contract):
    type: str
    title: str
    status: int = Field(ge=400, le=599)
    detail: str
    code: Literal["INVALID_FIELD", "UNAUTHENTICATED", "SOURCE_UNAVAILABLE"]
    request_id: str
    instance: str | None = None
    errors: list[dict[str, str]] = Field(default_factory=list)
    retryable: bool = False


def problem_response(problem: Problem) -> JSONResponse:
    return JSONResponse(
        problem.model_dump(mode="json"),
        status_code=problem.status,
        media_type="application/problem+json",
    )


async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return problem_response(
        Problem(
            type="urn:orca:problem:validation",
            title="Invalid request",
            status=422,
            detail="Request does not match its schema",
            code="INVALID_FIELD",
            request_id=request.state.request_id,
        )
    )
