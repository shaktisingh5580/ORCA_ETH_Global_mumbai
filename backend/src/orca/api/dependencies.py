"""Purpose: convert verified bearer tokens into request principals and tenant grants.
Behavior: reject missing/invalid tokens without trusting organization headers.
Refs: API sections 2.2,3.2,16; main plan P02.2; Phase: P02.
"""

from typing import Annotated, Protocol, cast

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError

from orca.domain.identity import Principal


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Principal: ...


oidc_bearer = HTTPBearer(auto_error=False, scheme_name="OIDC")


def current_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(oidc_bearer)],
) -> Principal:
    verifier = cast(TokenVerifier | None, getattr(request.app.state, "oidc_verifier", None))
    if verifier is None:
        raise HTTPException(status_code=503, detail="OIDC is not configured")
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401, detail="Bearer token required", headers={"WWW-Authenticate": "Bearer"}
        )
    try:
        return verifier.verify(credentials.credentials)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=401, detail="Invalid bearer token", headers={"WWW-Authenticate": "Bearer"}
        ) from exc


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
