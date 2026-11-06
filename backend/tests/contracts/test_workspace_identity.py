"""Purpose: prevent organization/tenant ID confusion and unusable single-role research workspaces.
Behavior: assert public project identity and role/token intersection for research operations.
Refs: API 3.2/3.3; platform completion audit; Phase: P02 repair.
"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from orca.api.routers.projects import project_json, require_scope
from orca.domain.identity import Principal
from orca.infrastructure.db.models import Project


def test_project_response_uses_organization_id_not_internal_tenant():
    organization, tenant = uuid4(), uuid4()
    project = Project(
        id=uuid4(),
        tenant_id=tenant,
        name="Research",
        slug="research",
        access_class="project",
        state="active",
        version=1,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    result = project_json(project, organization)
    assert result["organization_id"] == str(organization)
    assert result["organization_id"] != str(tenant)


def test_research_administrators_retain_research_capabilities_but_token_limits_apply():
    principal = Principal("test", "test", frozenset({"dataset:read", "research:plan"}))
    for role in {"org_owner", "org_admin", "principal_investigator", "researcher"}:
        principal.require("dataset:read", {role})
        principal.require("research:plan", {role})
    with pytest.raises(PermissionError):
        principal.require("research:plan", {"viewer"})
    with pytest.raises(PermissionError):
        Principal("test", "test", frozenset()).require("research:plan", {"org_owner"})
    with pytest.raises(HTTPException) as error:
        require_scope(principal, "project:write", {"reviewer"})
    assert error.value.status_code == 403
