"""Purpose: represent authenticated principals and bounded ORCA authorization scopes.
Behavior: derive capabilities from server-owned roles and intersect token-requested scopes.
Refs: API sections 2.2,3.2,16; main plan P02.2; Phase: P02.
"""

from dataclasses import dataclass
from typing import Final

ROLE_SCOPES: Final[dict[str, frozenset[str]]] = {
    "source_operator": frozenset({"org:read", "source:manage"}),
    "org_owner": frozenset(
        {
            "org:read",
            "org:write",
            "org:members",
            "project:read",
            "project:write",
            "project:share",
            "dataset:read",
            "research:plan",
            "research:run",
            "evidence:read",
            "evidence:write",
            "report:publish",
        }
    ),
    "org_admin": frozenset(
        {
            "org:read",
            "org:write",
            "org:members",
            "project:read",
            "project:write",
            "project:share",
            "dataset:read",
            "research:plan",
            "research:run",
            "evidence:read",
            "evidence:write",
            "report:publish",
        }
    ),
    "principal_investigator": frozenset(
        {
            "org:read",
            "project:read",
            "project:write",
            "project:share",
            "dataset:read",
            "research:plan",
            "research:run",
            "evidence:read",
            "evidence:write",
            "report:publish",
        }
    ),
    "researcher": frozenset(
        {
            "org:read",
            "project:read",
            "project:write",
            "dataset:read",
            "research:plan",
            "evidence:read",
        }
    ),
    "reviewer": frozenset(
        {"org:read", "project:read", "dataset:read", "evidence:read", "review:write"}
    ),
    "viewer": frozenset({"org:read", "project:read", "evidence:read"}),
}


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    issuer: str
    token_scopes: frozenset[str]
    verified_email: str | None = None

    def effective_scopes(self, roles: set[str]) -> frozenset[str]:
        allowed = frozenset().union(*(ROLE_SCOPES.get(role, frozenset()) for role in roles))
        return self.token_scopes & allowed

    def require(self, scope: str, roles: set[str]) -> None:
        if scope not in self.effective_scopes(roles):
            raise PermissionError(f"Required scope is not granted: {scope}")
