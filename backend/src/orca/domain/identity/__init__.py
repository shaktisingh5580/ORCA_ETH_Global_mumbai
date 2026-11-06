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
        }
    ),
    "researcher": frozenset(
        {"org:read", "project:read", "project:write", "dataset:read", "research:plan"}
    ),
    "reviewer": frozenset({"org:read", "project:read", "dataset:read"}),
    "viewer": frozenset({"org:read", "project:read"}),
}


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    issuer: str
    token_scopes: frozenset[str]

    def effective_scopes(self, roles: set[str]) -> frozenset[str]:
        allowed = frozenset().union(*(ROLE_SCOPES.get(role, frozenset()) for role in roles))
        return self.token_scopes & allowed

    def require(self, scope: str, roles: set[str]) -> None:
        if scope not in self.effective_scopes(roles):
            raise PermissionError(f"Required scope is not granted: {scope}")
